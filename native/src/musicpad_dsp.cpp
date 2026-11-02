// Shared DSP Core implementation for MusicPad
// Zero allocations, zero locks, real-time safe for audiodg.exe and ctypes Virtual Cable mode.

#include "../include/musicpad_dsp.h"
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <atomic>
#include <cmath>
#include <cstdio>
#include <cstring>

static inline float ClampSample(float x) {
    if (x > 1.0f) return 1.0f;
    if (x < -1.0f) return -1.0f;
    return x;
}

// Limiter threshold: -1.0 dBFS = 10^(-1/20) approx 0.8912509f
static const float MPAD_LIMITER_THRESH = 0.891250938f;
static const float MPAD_EXPANDER_THRESH = 0.0017782794f; // -55 dBFS

static inline float SoftLimiter(float x) {
    float abs_x = std::fabs(x);
    if (abs_x <= MPAD_LIMITER_THRESH) {
        return x;
    }
    float excess = abs_x - MPAD_LIMITER_THRESH;
    float headroom = 1.0f - MPAD_LIMITER_THRESH;
    float compressed = MPAD_LIMITER_THRESH + headroom * std::tanh(excess / headroom);
    if (compressed > 0.999f) compressed = 0.999f;
    return (x < 0.0f) ? -compressed : compressed;
}

// Internal DSP history for up to 8 channels (DC HPF + Downward Expander)
struct MpadChannelHistory {
    float hpf_x1 = 0.0f;
    float hpf_x2 = 0.0f;
    float hpf_y1 = 0.0f;
    float hpf_y2 = 0.0f;
    float env = 0.0f;
    float smooth_gain = 1.0f;
};

static MpadChannelHistory s_voice_history[8] = {};

// DeepFilterNet C API bindings and FIFO state for realtime APO / DSP execution
typedef void* (*fn_df_create)(const char*, float, const char*);
typedef float (*fn_df_process_frame)(void*, const float*, float*);
typedef void  (*fn_df_free)(void*);

static HMODULE s_df_dll = NULL;
static fn_df_create s_df_create = nullptr;
static fn_df_process_frame s_df_process_frame = nullptr;
static fn_df_free s_df_free = nullptr;
static void* s_df_state = nullptr;
static bool s_df_init_attempted = false;

static const uint32_t DF_FRAME_SIZE = 480;
static const uint32_t DF_FIFO_CAP = 8192;
static float s_df_fifo_in[DF_FIFO_CAP] = {};
static uint32_t s_df_fifo_in_count = 0;
static float s_df_fifo_out[DF_FIFO_CAP] = {};
static uint32_t s_df_fifo_out_count = 0;

static bool CheckFileExists(const char* path) {
    if (!path || !path[0]) return false;
    DWORD attr = GetFileAttributesA(path);
    return (attr != INVALID_FILE_ATTRIBUTES && !(attr & FILE_ATTRIBUTE_DIRECTORY));
}

static void InitDeepFilter() {
    if (s_df_init_attempted) return;
    s_df_init_attempted = true;

    char baseDir[MAX_PATH] = {};
    HMODULE hMod = NULL;
    if (GetModuleHandleExA(
            GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
            (LPCSTR)&mpad_dsp_process_voice,
            &hMod) && hMod) {
        if (GetModuleFileNameA(hMod, baseDir, MAX_PATH)) {
            char* lastSlash = strrchr(baseDir, '\\');
            if (lastSlash) {
                *lastSlash = '\0';
            }
        }
    }

    char dllPath[MAX_PATH] = {};
    const char* dllCandidates[] = {
        baseDir,
        "C:\\Windows\\System32",
        "C:\\Program Files\\MusicPad",
        "native\\bin",
        "."
    };

    for (const char* dir : dllCandidates) {
        if (!dir || !dir[0]) continue;
        char cand[MAX_PATH];
        snprintf(cand, sizeof(cand), "%s\\deepfilter.dll", dir);
        if (CheckFileExists(cand)) {
            snprintf(dllPath, sizeof(dllPath), "%s", cand);
            break;
        }
    }

    if (!dllPath[0]) {
        snprintf(dllPath, sizeof(dllPath), "deepfilter.dll");
    }

    s_df_dll = LoadLibraryA(dllPath);
    if (!s_df_dll) {
        return;
    }

    s_df_create = reinterpret_cast<fn_df_create>(reinterpret_cast<void(*)()>(GetProcAddress(s_df_dll, "df_create")));
    s_df_process_frame = reinterpret_cast<fn_df_process_frame>(reinterpret_cast<void(*)()>(GetProcAddress(s_df_dll, "df_process_frame")));
    s_df_free = reinterpret_cast<fn_df_free>(reinterpret_cast<void(*)()>(GetProcAddress(s_df_dll, "df_free")));

    if (!s_df_create || !s_df_process_frame || !s_df_free) {
        FreeLibrary(s_df_dll);
        s_df_dll = NULL;
        return;
    }

    char modelPath[MAX_PATH] = {};
    const char* modelCandidates[] = {
        baseDir,
        "C:\\Windows\\System32",
        "C:\\Program Files\\MusicPad",
        "native\\bin",
        "."
    };

    for (const char* dir : modelCandidates) {
        if (!dir || !dir[0]) continue;
        char cand[MAX_PATH];
        snprintf(cand, sizeof(cand), "%s\\DeepFilterNet3_onnx.tar.gz", dir);
        if (CheckFileExists(cand)) {
            snprintf(modelPath, sizeof(modelPath), "%s", cand);
            break;
        }
    }

    if (!modelPath[0]) {
        FreeLibrary(s_df_dll);
        s_df_dll = NULL;
        return;
    }

    s_df_state = s_df_create(modelPath, 100.0f, "error");
}

extern "C" {

MPAD_DSP_API void mpad_dsp_reset_voice_state(void) {
    for (int i = 0; i < 8; ++i) {
        s_voice_history[i].hpf_x1 = 0.0f;
        s_voice_history[i].hpf_x2 = 0.0f;
        s_voice_history[i].hpf_y1 = 0.0f;
        s_voice_history[i].hpf_y2 = 0.0f;
        s_voice_history[i].env = 0.0f;
        s_voice_history[i].smooth_gain = 1.0f;
    }
    s_df_fifo_in_count = 0;
    s_df_fifo_out_count = 0;
}

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
    (void)fx_param2;

    if (!samples || frames == 0 || channels == 0) {
        return;
    }

    const uint32_t total_samples = frames * channels;

    // Mic Mute check
    if (mic_mute != 0) {
        std::memset(samples, 0, (size_t)total_samples * sizeof(float));
        return;
    }

    const float sr = (sample_rate > 8000.0f) ? sample_rate : 48000.0f;
    const uint32_t ch_limit = (channels <= 8) ? channels : 8;

    // Stage 1: DeepFilterNet (wet/dry)
    if ((voice_fx_flags & MPAD_FX_DEEPFILTER) != 0) {
        if (!s_df_init_attempted) {
            InitDeepFilter();
        }
        if (s_df_state && s_df_process_frame) {
            float wet = fx_param1;
            if (wet < 0.0f) wet = 0.0f;
            if (wet > 1.0f) wet = 1.0f;

            if (wet > 0.0f) {
                float clean_mono[2048];
                float dry_mono[2048];
                const uint32_t proc_frames = (frames <= 2048) ? frames : 2048;

                // 1. Extract mono signal
                for (uint32_t f = 0; f < proc_frames; ++f) {
                    if (channels == 1) {
                        dry_mono[f] = samples[f];
                    } else {
                        float sum = 0.0f;
                        for (uint32_t c = 0; c < channels; ++c) {
                            sum += samples[f * channels + c];
                        }
                        dry_mono[f] = sum / (float)channels;
                    }
                }

                // 2. Append to input FIFO
                uint32_t space_in = (s_df_fifo_in_count < DF_FIFO_CAP) ? (DF_FIFO_CAP - s_df_fifo_in_count) : 0;
                uint32_t to_push = (proc_frames <= space_in) ? proc_frames : space_in;
                if (to_push > 0) {
                    std::memcpy(&s_df_fifo_in[s_df_fifo_in_count], dry_mono, (size_t)to_push * sizeof(float));
                    s_df_fifo_in_count += to_push;
                }

                // 3. Process complete 480-sample blocks
                while (s_df_fifo_in_count >= DF_FRAME_SIZE) {
                    float out_frame[DF_FRAME_SIZE];
                    s_df_process_frame(s_df_state, s_df_fifo_in, out_frame);

                    s_df_fifo_in_count -= DF_FRAME_SIZE;
                    if (s_df_fifo_in_count > 0) {
                        std::memmove(s_df_fifo_in, &s_df_fifo_in[DF_FRAME_SIZE], (size_t)s_df_fifo_in_count * sizeof(float));
                    }

                    uint32_t space_out = (s_df_fifo_out_count < DF_FIFO_CAP) ? (DF_FIFO_CAP - s_df_fifo_out_count) : 0;
                    uint32_t to_out = (DF_FRAME_SIZE <= space_out) ? DF_FRAME_SIZE : space_out;
                    if (to_out > 0) {
                        std::memcpy(&s_df_fifo_out[s_df_fifo_out_count], out_frame, (size_t)to_out * sizeof(float));
                        s_df_fifo_out_count += to_out;
                    }
                }

                // 4. Retrieve clean mono
                if (s_df_fifo_out_count >= proc_frames) {
                    std::memcpy(clean_mono, s_df_fifo_out, (size_t)proc_frames * sizeof(float));
                    s_df_fifo_out_count -= proc_frames;
                    if (s_df_fifo_out_count > 0) {
                        std::memmove(s_df_fifo_out, &s_df_fifo_out[proc_frames], (size_t)s_df_fifo_out_count * sizeof(float));
                    }
                } else {
                    std::memcpy(clean_mono, dry_mono, (size_t)proc_frames * sizeof(float));
                }

                // 5. Wet/Dry blend into output samples
                if (wet >= 1.0f) {
                    for (uint32_t f = 0; f < proc_frames; ++f) {
                        for (uint32_t c = 0; c < channels; ++c) {
                            samples[f * channels + c] = clean_mono[f];
                        }
                    }
                } else {
                    for (uint32_t f = 0; f < proc_frames; ++f) {
                        for (uint32_t c = 0; c < channels; ++c) {
                            float orig = samples[f * channels + c];
                            samples[f * channels + c] = (1.0f - wet) * orig + wet * clean_mono[f];
                        }
                    }
                }
            }
        }
    } else {
        if (s_df_fifo_in_count > 0 || s_df_fifo_out_count > 0) {
            s_df_fifo_in_count = 0;
            s_df_fifo_out_count = 0;
        }
    }

    // Stage 2: Усунення білого шуму (DC Offset HPF + Downward Expander)
    if ((voice_fx_flags & MPAD_FX_WHITE_NOISE) != 0) {
        // High-pass filter 35 Hz (2nd order Butterworth)
        const float K = std::tan(3.141592653589793f * 35.0f / sr);
        const float Q = 0.7071067811865475f;
        const float norm = 1.0f / (1.0f + K / Q + K * K);
        const float b0 = norm;
        const float b1 = -2.0f * b0;
        const float b2 = b0;
        const float a1 = 2.0f * (K * K - 1.0f) * norm;
        const float a2 = (1.0f - K / Q + K * K) * norm;

        // Downward Expander attack (8 ms) and release (180 ms) coefficients
        const float alpha_att = 1.0f - std::exp(-1.0f / (sr * 0.008f));
        const float alpha_rel = 1.0f - std::exp(-1.0f / (sr * 0.180f));
        const float thresh = MPAD_EXPANDER_THRESH;

        for (uint32_t f = 0; f < frames; ++f) {
            for (uint32_t c = 0; c < channels; ++c) {
                const uint32_t idx = f * channels + c;
                float x = samples[idx];
                MpadChannelHistory& st = s_voice_history[(c < ch_limit) ? c : 0];

                // 2.1 HPF for DC offset
                float y = b0 * x + b1 * st.hpf_x1 + b2 * st.hpf_x2 - a1 * st.hpf_y1 - a2 * st.hpf_y2;
                if (std::fabs(y) < 1e-15f) y = 0.0f;
                st.hpf_x2 = st.hpf_x1;
                st.hpf_x1 = x;
                st.hpf_y2 = st.hpf_y1;
                st.hpf_y1 = y;

                // 2.2 Envelope follower
                float abs_y = std::fabs(y);
                if (abs_y > st.env) {
                    st.env += alpha_att * (abs_y - st.env);
                } else {
                    st.env += alpha_rel * (abs_y - st.env);
                }

                // 2.3 Downward Expansion: only silence below threshold is pulled down smoothly
                float target_gain = 1.0f;
                if (st.env < thresh) {
                    float r = st.env / thresh;
                    target_gain = r * r;
                }
                st.smooth_gain += 0.01f * (target_gain - st.smooth_gain);
                if (st.smooth_gain < 1e-4f) st.smooth_gain = 0.0f;
                y *= st.smooth_gain;

                samples[idx] = y;
            }
        }
    }

    // Stage 3 & 4: Voice Boost (0..+24 dB) and Limiter (-1.0 dBFS)
    const bool boost_enabled = (voice_fx_flags & MPAD_FX_VOICE_BOOST) != 0;
    float gain = mic_gain;
    if (gain < 0.0f) gain = 0.0f;
    if (boost_enabled) {
        if (gain > 16.0f) gain = 16.0f;
    } else {
        if (gain > 4.0f) gain = 4.0f;
    }

    if (gain != 1.0f) {
        for (uint32_t i = 0; i < total_samples; ++i) {
            samples[i] *= gain;
        }
    }

    if (boost_enabled) {
        for (uint32_t i = 0; i < total_samples; ++i) {
            samples[i] = SoftLimiter(samples[i]);
        }
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
