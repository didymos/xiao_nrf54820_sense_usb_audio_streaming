/*
 * XIAO nRF52840 Sense — USB Audio Class 1 Microphone
 *
 * Audio pipeline:
 *   MSM261D3526H1CPM (PDM) ──► nrfx_pdm (HW CIC+FIR, 64× decimation)
 *   ──► PCM 48 kHz / 16-bit / mono ──► USB ISO IN endpoint ──► host (Audacity)
 *
 * Design goals:
 *   - Minimum latency: 1 ms PDM blocks → immediate USB dispatch
 *   - No sample-rate conversion or software filters (hardware does it)
 *   - Double-buffered DMA so capture never stalls
 *   - Graceful USB connect/disconnect without restarting PDM
 */

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/audio/dmic.h>
#include <zephyr/usb/usb_device.h>
#include <zephyr/usb/class/usb_audio.h>
#include <zephyr/net/buf.h>
#include <string.h>

LOG_MODULE_REGISTER(usb_mic, LOG_LEVEL_INF);

/* ── Audio constants ──────────────────────────────────────────────────── */

#define SAMPLE_RATE_HZ    48000
#define SAMPLE_WIDTH_BITS 16
#define SAMPLE_WIDTH_BYTES (SAMPLE_WIDTH_BITS / 8)
#define CHANNELS          1

/*
 * Block size = 1 ms of PCM audio.
 * At 48 kHz mono 16-bit: 48 samples × 2 bytes = 96 bytes.
 * This matches the USB full-speed isochronous frame period exactly,
 * giving the lowest possible end-to-end latency.
 */
#define SAMPLES_PER_MS   (SAMPLE_RATE_HZ / 1000)
#define BLOCK_SAMPLES    (SAMPLES_PER_MS * CHANNELS)
#define BLOCK_SIZE_BYTES (BLOCK_SAMPLES * SAMPLE_WIDTH_BYTES)

/*
 * DMIC memory slab — holds N DMA-ready buffers.
 * 8 blocks = 8 ms of headroom; prevents xrun if USB is briefly busy.
 * Each buffer is 4-byte aligned as required by the nrfx_pdm DMA engine.
 */
#define DMIC_SLAB_BLOCKS 8
K_MEM_SLAB_DEFINE_STATIC(dmic_slab, BLOCK_SIZE_BYTES, DMIC_SLAB_BLOCKS, 4);

/*
 * net_buf pool for the USB audio class driver.
 * 4 buffers: one in-flight on the ISO endpoint, one being filled,
 * two spare to absorb timing jitter.
 */
#define USB_BUF_COUNT 4
NET_BUF_POOL_FIXED_DEFINE(usb_audio_pool, USB_BUF_COUNT,
			   BLOCK_SIZE_BYTES, 0, NULL);

/* ── Device handles ───────────────────────────────────────────────────── */

/* nRF52840 PDM peripheral, bound by nrfx_pdm Zephyr driver */
static const struct device *dmic_dev = DEVICE_DT_GET(DT_NODELABEL(pdm0));

/* USB Audio Class 1 microphone instance — use compat-based lookup so the
 * reference matches exactly how the driver calls DEVICE_DT_INST_DEFINE. */
static const struct device *mic_dev =
	DEVICE_DT_GET_ONE(zephyr_usb_audio_mic);

/* ── USB state ────────────────────────────────────────────────────────── */

static atomic_t usb_active = ATOMIC_INIT(0);

/* Mute state managed by OS mixer — apply per-block in the audio loop */
static atomic_t mic_muted = ATOMIC_INIT(0);

/* ── USB Audio callbacks ──────────────────────────────────────────────── */

/*
 * Called by USB Audio Class when the host changes a feature unit control
 * (mute, volume). We only act on mute here; volume is handled in hardware
 * by the nrfx_pdm gain registers, but that would require a dmic_configure()
 * cycle — simpler to zero-fill in software when muted.
 */
static void audio_feature_update(const struct device *dev,
				  const struct usb_audio_fu_evt *evt)
{
	if (evt->cs == USB_AUDIO_FU_MUTE_CONTROL) {
		bool muted = (evt->val != 0);
		atomic_set(&mic_muted, muted ? 1 : 0);
		LOG_DBG("Mic mute → %s", muted ? "ON" : "OFF");
	}
}

/* Not used for a microphone-only device (no audio OUT endpoint) */
static void audio_data_received(const struct device *dev,
				 struct net_buf *buffer, size_t size)
{
	net_buf_unref(buffer);
}

static const struct usb_audio_ops usb_ops = {
	.data_received_cb  = audio_data_received,
	.feature_update_cb = audio_feature_update,
};

/* ── USB bus-state callback ───────────────────────────────────────────── */

static void usb_status_cb(enum usb_dc_status_code status,
			   const uint8_t *param)
{
	switch (status) {
	case USB_DC_CONNECTED:
		LOG_INF("USB connected");
		break;
	case USB_DC_CONFIGURED:
		LOG_INF("USB configured — streaming");
		atomic_set(&usb_active, 1);
		break;
	case USB_DC_DISCONNECTED:
		LOG_INF("USB disconnected");
		atomic_set(&usb_active, 0);
		break;
	case USB_DC_RESET:
		LOG_WRN("USB reset");
		atomic_set(&usb_active, 0);
		break;
	case USB_DC_SUSPEND:
		LOG_INF("USB suspended");
		atomic_set(&usb_active, 0);
		break;
	case USB_DC_RESUME:
		LOG_INF("USB resumed");
		break;
	default:
		break;
	}
}

/* ── DMIC initialisation ──────────────────────────────────────────────── */

static int dmic_init(void)
{
	if (!device_is_ready(dmic_dev)) {
		LOG_ERR("PDM device not ready");
		return -ENODEV;
	}

	/*
	 * PDM clock budget for 48 kHz PCM with 64× decimation:
	 *   f_pdm = 48000 × 64 = 3.072 MHz
	 * The nrfx_pdm driver picks the nearest achievable clock from
	 * PCLK32M; on nRF52840 this is exact at 32 MHz / ~10.4 ≈ 3.072 MHz.
	 *
	 * Acceptable DC range: 40–60 % (mic datasheet §4.3).
	 */
	struct pcm_stream_cfg stream = {
		.pcm_rate   = SAMPLE_RATE_HZ,
		.pcm_width  = SAMPLE_WIDTH_BITS,
		.block_size = BLOCK_SIZE_BYTES,
		.mem_slab   = &dmic_slab,
	};

	struct dmic_cfg cfg = {
		.io = {
			.min_pdm_clk_freq = 1000000,  /* 1 MHz min */
			.max_pdm_clk_freq = 3500000,  /* 3.5 MHz max (mic spec) */
			.min_pdm_clk_dc   = 40,
			.max_pdm_clk_dc   = 60,
		},
		.streams        = &stream,
		.channel = {
			.req_num_streams = 1,
			.req_num_chan     = CHANNELS,
			/* Map stream 0, channel 0 to the LEFT PDM pin (DIN) */
			.req_chan_map_lo  =
				dmic_build_channel_map(0, 0, PDM_CHAN_LEFT),
		},
	};

	int ret = dmic_configure(dmic_dev, &cfg);
	if (ret) {
		LOG_ERR("dmic_configure() failed: %d", ret);
	}
	return ret;
}

/* ── Audio streaming thread ───────────────────────────────────────────── */

/*
 * This thread runs at kernel priority 2 (high, preemptive) to minimise
 * jitter in the PDM→USB handoff. It owns the entire audio path.
 *
 * Loop invariant: dmic_read() blocks until one 1 ms DMA buffer is ready,
 * then we immediately dispatch it to the USB ISO IN endpoint via
 * usb_audio_send(). If the host is not yet connected, we drain the slab
 * so the DMA engine never stalls.
 */
static void audio_thread(void *p1, void *p2, void *p3)
{
	ARG_UNUSED(p1);
	ARG_UNUSED(p2);
	ARG_UNUSED(p3);

	int ret;

	ret = dmic_trigger(dmic_dev, DMIC_TRIGGER_START);
	if (ret) {
		LOG_ERR("DMIC START trigger failed: %d", ret);
		return;
	}
	LOG_INF("PDM capture started (48 kHz / 16-bit / mono)");

	while (true) {
		void    *dmic_buf;
		uint32_t dmic_size;

		/*
		 * Block for up to 100 ms waiting for the next DMA buffer.
		 * Timeout is a safety net; normal cadence is 1 ms.
		 */
		ret = dmic_read(dmic_dev, 0, &dmic_buf, &dmic_size, 100);
		if (ret) {
			if (ret == -EAGAIN) {
				LOG_WRN("DMIC read timeout");
			} else {
				LOG_ERR("DMIC read error: %d", ret);
			}
			continue;
		}

		/* If USB host is not active, drain the slab and loop */
		if (!atomic_get(&usb_active)) {
			k_mem_slab_free(&dmic_slab, &dmic_buf);
			continue;
		}

		/* Grab a net_buf from the USB audio pool (non-blocking) */
		struct net_buf *usb_buf =
			net_buf_alloc(&usb_audio_pool, K_NO_WAIT);
		if (!usb_buf) {
			/*
			 * Pool exhausted — USB is backed up. Drop this frame.
			 * Better to skip one frame than to block the DMA path.
			 */
			LOG_WRN("USB buf pool empty — frame dropped");
			k_mem_slab_free(&dmic_slab, &dmic_buf);
			continue;
		}

		uint32_t send_size = MIN(dmic_size, BLOCK_SIZE_BYTES);

		if (atomic_get(&mic_muted)) {
			/* Host muted: send silence so the endpoint stays live */
			memset(net_buf_add(usb_buf, send_size), 0, send_size);
		} else {
			net_buf_add_mem(usb_buf, dmic_buf, send_size);
		}

		k_mem_slab_free(&dmic_slab, &dmic_buf);

		/*
		 * Hand the buffer to the USB Audio Class driver.
		 * On success the driver owns usb_buf and will unref it after
		 * the ISO IN transfer completes.
		 * On failure we must unref it ourselves.
		 */
		ret = usb_audio_send(mic_dev, usb_buf, send_size);
		if (ret) {
			net_buf_unref(usb_buf);
			/* -ENODEV or -EAGAIN are normal during USB negotiation */
			if (ret != -ENODEV && ret != -EAGAIN) {
				LOG_WRN("usb_audio_send: %d", ret);
			}
		}
	}
}

#define AUDIO_THREAD_STACK_SIZE 4096

K_THREAD_STACK_DEFINE(audio_stack, AUDIO_THREAD_STACK_SIZE);
static struct k_thread audio_thread_data;

/* ── Main ─────────────────────────────────────────────────────────────── */

int main(void)
{
	LOG_INF("XIAO nRF52840 Sense — USB Microphone v1.0");
	LOG_INF("  %d Hz / %d-bit / %d ch  |  block=%d bytes",
		SAMPLE_RATE_HZ, SAMPLE_WIDTH_BITS, CHANNELS,
		BLOCK_SIZE_BYTES);

	/* 1. Initialise PDM peripheral */
	int ret = dmic_init();
	if (ret) {
		LOG_ERR("DMIC init failed — halting");
		return ret;
	}

	/* 2. Register USB Audio callbacks before enabling USB */
	usb_audio_register(mic_dev, &usb_ops);

	/* 3. Enable USB device stack; nRF52840 USBD driver powers up the
	 *    internal USB regulator and waits for VBUS. */
	ret = usb_enable(usb_status_cb);
	if (ret) {
		LOG_ERR("usb_enable() failed: %d", ret);
		return ret;
	}
	LOG_INF("USB enabled — connect to host");

	/* 4. Start audio streaming thread at high kernel priority */
	k_thread_create(&audio_thread_data, audio_stack,
			K_THREAD_STACK_SIZEOF(audio_stack),
			audio_thread,
			NULL, NULL, NULL,
			2,        /* priority: high (lower number = higher prio) */
			0,        /* options */
			K_NO_WAIT);
	k_thread_name_set(&audio_thread_data, "audio_stream");

	return 0;
}
