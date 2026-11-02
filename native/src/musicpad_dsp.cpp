// Shared DSP Core implementation for MusicPad
// Zero allocations, zero locks, real-time safe for audiodg.exe and ctypes Virtual Cable mode.

#include "../include/musicpad_dsp.h"
#include <atomic>
#include <cstring>

static inline float ClampSample(float x) {
    if (x > 1.0f) return 1.0f;
    if (x < -1.0f) return -1.0f;
    return x;
}

extern "C" {

MPAD_DSP_API void mpad_dsp_clamp_inplace(float* samples, uint32_t sample_count) {
    if (!samples) return;
    for (uint32_t i = 0; i < sample_count; ++i) {
        samples[i] = ClampSample(samples[i]);
    }
}

MPAD_DSP_API void mpad_dsp_process_voice(
    float*   samples,
    uint32_t frames,
    uint32_t channels,
    float    sample_rate,
    float    mic_gain,
    int      mic_mute,
    uint32_t voice_fx_flags,
    float    fx_param1,
    float    fx_param2)
{
    (void)sample_rate;
    (void)fx_param1;
    (void)fx_param2;

    if (!samples || frames == 0 || channels == 0) {
        return;
    }

    const uint32_t total_samples = frames * channels;

    // Stage 1: Microphone Mute / Gain
    if (mic_mute != 0) {
        std::memset(samples, 0, (size_t)total_samples * sizeof(float));
        return;
    }

    float gain = mic_gain;
    if (gain < 0.0f) gain = 0.0f;
    if (gain > 4.0f) gain = 4.0f;

    if (gain != 1.0f) {
        for (uint32_t i = 0; i < total_samples; ++i) {
            samples[i] *= gain;
        }
    }

    // Stage 2: Voice FX Pipeline Hook (reserved for future WebRTC APM / DeepFilterNet stages)
    if (voice_fx_flags != MPAD_FX_NONE) {
        // Future stages plug in here in sequence:
        //   if (voice_fx_flags & MPAD_FX_HPF)  { ... }
        //   if (voice_fx_flags & MPAD_FX_NR)   { ... }
        //   if (voice_fx_flags & MPAD_FX_AGC2) { ... }
    }
}

MPAD_DSP_API void mpad_dsp_mix_inplace(
    float*       voice_inout,
    const float* track_in,
    uint32_t     sample_count)
{
    if (!voice_inout) return;
    if (!track_in) {
        mpad_dsp_clamp_inplace(voice_inout, sample_count);
        return;
    }
    for (uint32_t i = 0; i < sample_count; ++i) {
        voice_inout[i] = ClampSample(voice_inout[i] + track_in[i]);
    }
}

MPAD_DSP_API uint32_t mpad_shm_available_to_read(const MusicPadShmLayout* shm) {
    if (!shm) return 0;
    const uint32_t cap = (shm->header.capacity_samples > 0 && shm->header.capacity_samples <= MPAD_RING_CAPACITY_SAMPLES)
        ? shm->header.capacity_samples
        : MPAD_RING_CAPACITY_SAMPLES;
    const uint64_t w = shm->header.write_sample_idx;
    const uint64_t r = shm->header.read_sample_idx;
    std::atomic_thread_fence(std::memory_order_acquire);
    if (w <= r) return 0;
    const uint64_t avail = w - r;
    return (avail > cap) ? cap : (uint32_t)avail;
}

MPAD_DSP_API uint32_t mpad_shm_available_to_write(const MusicPadShmLayout* shm) {
    if (!shm) return 0;
    const uint32_t cap = (shm->header.capacity_samples > 0 && shm->header.capacity_samples <= MPAD_RING_CAPACITY_SAMPLES)
        ? shm->header.capacity_samples
        : MPAD_RING_CAPACITY_SAMPLES;
    const uint32_t used = mpad_shm_available_to_read(shm);
    return (used >= cap) ? 0 : (cap - used);
}

MPAD_DSP_API uint32_t mpad_shm_write_samples(
    MusicPadShmLayout* shm,
    const float*       samples,
    uint32_t           sample_count)
{
    if (!shm || !samples || sample_count == 0) return 0;

    const uint32_t cap = (shm->header.capacity_samples > 0 && shm->header.capacity_samples <= MPAD_RING_CAPACITY_SAMPLES)
        ? shm->header.capacity_samples
        : MPAD_RING_CAPACITY_SAMPLES;

    const uint64_t r = shm->header.read_sample_idx;
    uint64_t w = shm->header.write_sample_idx;
    std::atomic_thread_fence(std::memory_order_acquire);

    if (w < r || (w - r) > cap) {
        w = r;
    }

    const uint32_t used = (uint32_t)(w - r);
    const uint32_t free_slots = (used >= cap) ? 0 : (cap - used);
    uint32_t to_write = (sample_count <= free_slots) ? sample_count : free_slots;

    const uint32_t ch = (shm->header.apo_channels > 0) ? shm->header.apo_channels : 2;
    if (ch > 1) {
        to_write = (to_write / ch) * ch;
    }
    if (to_write == 0) {
        return 0;
    }

    const uint32_t write_pos = (uint32_t)(w % cap);
    const uint32_t first_chunk = (write_pos + to_write <= cap) ? to_write : (cap - write_pos);
    const uint32_t second_chunk = to_write - first_chunk;

    std::memcpy(&shm->samples[write_pos], samples, (size_t)first_chunk * sizeof(float));
    if (second_chunk > 0) {
        std::memcpy(&shm->samples[0], samples + first_chunk, (size_t)second_chunk * sizeof(float));
    }

    std::atomic_thread_fence(std::memory_order_release);
    shm->header.write_sample_idx = w + to_write;
    return to_write;
}

MPAD_DSP_API int mpad_dsp_process_shm_block(
    MusicPadShmLayout* shm,
    const float*       mic_in,
    float*             mic_out,
    uint32_t           frames,
    uint32_t           channels,
    float              sample_rate,
    int64_t            now_ns,
    uint32_t*          inout_last_flush_seq)
{
    if (!mic_in || !mic_out || frames == 0 || channels == 0) {
        return 0;
    }

    const uint32_t total_samples = frames * channels;

    if (!shm) {
        if (mic_out != mic_in) {
            std::memcpy(mic_out, mic_in, (size_t)total_samples * sizeof(float));
        }
        return 0;
    }

    // Update APO telemetry / heartbeat even when in passthrough so MusicPad can see the driver is alive
    shm->header.apo_build_version = MPAD_APO_BUILD_VERSION;
    shm->header.apo_sample_rate   = (uint32_t)(sample_rate > 0.0f ? (sample_rate + 0.5f) : 48000);
    shm->header.apo_channels      = channels;
    shm->header.apo_heartbeat_ns  = now_ns;
    shm->header.apo_processed_blocks += 1;
    std::atomic_thread_fence(std::memory_order_acq_rel);

    const uint32_t magic        = shm->header.magic;
    const uint32_t version      = shm->header.protocol_version;
    const uint32_t active_flags = shm->header.active_flags;
    const int64_t  app_hb_ns    = shm->header.app_heartbeat_ns;

    const bool header_valid = (magic == MPAD_SHM_MAGIC) && (version == MPAD_SHM_VERSION);
    const bool app_active   = (active_flags & MPAD_FLAG_APP_ACTIVE) != 0 &&
                              (active_flags & MPAD_FLAG_PASSTHROUGH) == 0;
    const int64_t hb_delta  = now_ns - app_hb_ns;
    const bool hb_fresh     = (app_hb_ns > 0) && (hb_delta >= -MPAD_HEARTBEAT_TIMEOUT_NS) && (hb_delta <= MPAD_HEARTBEAT_TIMEOUT_NS);

    // Pure passthrough when app is closed, inactive, or heartbeat expired (> 300 ms)
    if (!header_valid || !app_active || !hb_fresh) {
        if (mic_out != mic_in) {
            std::memcpy(mic_out, mic_in, (size_t)total_samples * sizeof(float));
        }
        return 0;
    }

    // Copy input voice if not already in-place
    if (mic_out != mic_in) {
        std::memcpy(mic_out, mic_in, (size_t)total_samples * sizeof(float));
    }

    // Run staged voice pipeline (mute / gain / voice FX)
    const int mic_mute = (active_flags & MPAD_FLAG_MIC_MUTE) ? 1 : 0;
    mpad_dsp_process_voice(
        mic_out,
        frames,
        channels,
        sample_rate,
        shm->header.mic_gain,
        mic_mute,
        shm->header.voice_fx_flags,
        shm->header.voice_fx_param1,
        shm->header.voice_fx_param2
    );

    // Check if the producer requested an immediate ring buffer flush (e.g. Stop / track change)
    const uint32_t cap = (shm->header.capacity_samples > 0 && shm->header.capacity_samples <= MPAD_RING_CAPACITY_SAMPLES)
        ? shm->header.capacity_samples
        : MPAD_RING_CAPACITY_SAMPLES;

    const uint64_t w = shm->header.write_sample_idx;
    uint64_t r = shm->header.read_sample_idx;
    std::atomic_thread_fence(std::memory_order_acquire);

    if (inout_last_flush_seq) {
        const uint32_t current_flush = shm->header.flush_seq;
        if (current_flush != *inout_last_flush_seq) {
            *inout_last_flush_seq = current_flush;
            r = w;
            shm->header.read_sample_idx = r;
        }
    }

    // Guard against corrupted or overflowed cursor distance
    if (r > w || (w - r) > cap) {
        r = w;
        shm->header.read_sample_idx = r;
    }

    const uint64_t avail64 = w - r;
    // Align available samples to whole multi-channel frames
    const uint32_t avail_samples = (uint32_t)((avail64 / channels) * channels);
    const uint32_t to_mix = (avail_samples >= total_samples) ? total_samples : avail_samples;

    if (to_mix > 0) {
        const uint32_t read_pos = (uint32_t)(r % cap);
        const uint32_t first_chunk = (read_pos + to_mix <= cap) ? to_mix : (cap - read_pos);
        const uint32_t second_chunk = to_mix - first_chunk;

        const bool need_fade = (to_mix < total_samples);
        const uint32_t total_frames = (channels > 0) ? (to_mix / channels) : 0;
        const uint32_t fade_frames = (need_fade && total_frames > 16) ? 16 : 0;
        const uint32_t fade_start_sample = (fade_frames > 0) ? (to_mix - fade_frames * channels) : to_mix;

        for (uint32_t i = 0; i < first_chunk; ++i) {
            float track_s = shm->samples[read_pos + i];
            if (i >= fade_start_sample && fade_frames > 0) {
                const uint32_t sample_in_fade = i - fade_start_sample;
                const uint32_t frame_in_fade = sample_in_fade / channels;
                float fade = 1.0f - ((float)(frame_in_fade + 1) / (float)fade_frames);
                if (fade < 0.0f) fade = 0.0f;
                track_s *= fade;
            }
            mic_out[i] = ClampSample(mic_out[i] + track_s);
        }
        for (uint32_t i = 0; i < second_chunk; ++i) {
            const uint32_t global_i = first_chunk + i;
            float track_s = shm->samples[i];
            if (global_i >= fade_start_sample && fade_frames > 0) {
                const uint32_t sample_in_fade = global_i - fade_start_sample;
                const uint32_t frame_in_fade = sample_in_fade / channels;
                float fade = 1.0f - ((float)(frame_in_fade + 1) / (float)fade_frames);
                if (fade < 0.0f) fade = 0.0f;
                track_s *= fade;
            }
            mic_out[global_i] = ClampSample(mic_out[global_i] + track_s);
        }

        std::atomic_thread_fence(std::memory_order_release);
        shm->header.read_sample_idx = r + to_mix;
    }

    // Clamp any remaining voice-only samples (if to_mix < total_samples)
    for (uint32_t i = to_mix; i < total_samples; ++i) {
        mic_out[i] = ClampSample(mic_out[i]);
    }

    // Record underrun if track playback is flagged active and buffer did not have enough frames
    if (to_mix < total_samples && (active_flags & MPAD_FLAG_TRACK_PLAYING) != 0) {
        shm->header.underrun_count += 1;
    }

    return 1;
}

} // extern "C"
