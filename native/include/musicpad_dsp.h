// Shared DSP Core for MusicPad
// Linked into musicpad_apo.dll (Injection mode) and exported in musicpad_dsp.dll (Virtual Cable mode via ctypes)

#ifndef MUSICPAD_DSP_H
#define MUSICPAD_DSP_H

#include "musicpad_shm.h"

#ifdef _WIN32
  #ifdef MPAD_DSP_BUILD_DLL
    #define MPAD_DSP_API __declspec(dllexport)
  #else
    #define MPAD_DSP_API
  #endif
#else
  #define MPAD_DSP_API
#endif

#ifdef __cplusplus
extern "C" {
#endif

// Process voice buffer in-place through the staged voice pipeline:
//   Stage 1: Microphone mute / gain
//   Stage 2: Voice processing chain (HPF / NR / AGC2 hooks, controlled via voice_fx_flags)
MPAD_DSP_API void mpad_dsp_process_voice(
    float*   samples,
    uint32_t frames,
    uint32_t channels,
    float    sample_rate,
    float    mic_gain,
    int      mic_mute,
    uint32_t voice_fx_flags,
    float    fx_param1,
    float    fx_param2
);

// Mix track samples into voice buffer in-place with [-1.0, 1.0] clamping.
MPAD_DSP_API void mpad_dsp_mix_inplace(
    float*       voice_inout,
    const float* track_in,
    uint32_t     sample_count
);

// Reset internal voice pipeline filter/expander history.
MPAD_DSP_API void mpad_dsp_reset_voice_state(void);

// Clamp float32 buffer in-place to [-1.0, 1.0].
MPAD_DSP_API void mpad_dsp_clamp_inplace(
    float*   samples,
    uint32_t sample_count
);

// Query number of unread float32 samples currently buffered in the SPSC ring.
MPAD_DSP_API uint32_t mpad_shm_available_to_read(const MusicPadShmLayout* shm);

// Query number of free float32 sample slots available for writing in the SPSC ring.
MPAD_DSP_API uint32_t mpad_shm_available_to_write(const MusicPadShmLayout* shm);

// Write float32 samples into the SPSC ring buffer (producer side).
// Returns the number of float32 samples actually written.
MPAD_DSP_API uint32_t mpad_shm_write_samples(
    MusicPadShmLayout* shm,
    const float*       samples,
    uint32_t           sample_count
);

// Real-time APO block processor operating on shared memory.
// Returns 1 if active processing (gain/mute/mix) was applied, or 0 if pure passthrough occurred.
MPAD_DSP_API int mpad_dsp_process_shm_block(
    MusicPadShmLayout* shm,
    const float*       mic_in,
    float*             mic_out,
    uint32_t           frames,
    uint32_t           channels,
    float              sample_rate,
    int64_t            now_ns,
    uint32_t*          inout_last_flush_seq
);

#ifdef __cplusplus
}
#endif

#endif // MUSICPAD_DSP_H
