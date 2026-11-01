// Shared Memory Layout & Protocol Contract between MusicPad (Python/C) and MusicPadAPO (audiodg.exe)
// File path: C:\ProgramData\MusicPad\injection.bin

#ifndef MUSICPAD_SHM_H
#define MUSICPAD_SHM_H

#include <stdint.h>
#include <stddef.h>

#ifdef _WIN32
#include <wchar.h>
#endif

#ifdef __cplusplus
extern "C" {
#endif

#define MPAD_SHM_PATH_W             L"C:\\ProgramData\\MusicPad\\injection.bin"
#define MPAD_DATA_DIR_W             L"C:\\ProgramData\\MusicPad"

// 'MPAD' in little-endian
#define MPAD_SHM_MAGIC              0x4441504Du
#define MPAD_SHM_VERSION            1u
#define MPAD_APO_BUILD_VERSION      0x00010000u

// Active flags bitmask (MusicPadShmHeader::active_flags)
#define MPAD_FLAG_APP_ACTIVE        (1u << 0)  // MusicPad is running in Injection mode
#define MPAD_FLAG_MIC_MUTE          (1u << 1)  // Mute physical microphone voice before mixing track
#define MPAD_FLAG_PASSTHROUGH       (1u << 2)  // Force pure passthrough (bypass gain, mute, and mix)
#define MPAD_FLAG_TRACK_PLAYING     (1u << 3)  // Track is currently playing (used for underrun counting)

// Voice FX flags bitmask (MusicPadShmHeader::voice_fx_flags, reserved for future stages)
#define MPAD_FX_NONE                0u
#define MPAD_FX_HPF                 (1u << 0)
#define MPAD_FX_NR                  (1u << 1)
#define MPAD_FX_AGC2                (1u << 2)

// Heartbeat timeout: 300 ms in nanoseconds (300,000,000 ns)
#define MPAD_HEARTBEAT_TIMEOUT_NS   300000000LL

// Ring buffer capacity: 96,000 float32 samples (1.0 second of stereo @ 48 kHz = 384,000 bytes)
#define MPAD_RING_CAPACITY_SAMPLES  96000u
#define MPAD_HEADER_SIZE_BYTES      512u
#define MPAD_SHM_TOTAL_BYTES        (MPAD_HEADER_SIZE_BYTES + (MPAD_RING_CAPACITY_SAMPLES * sizeof(float)))

#pragma pack(push, 1)
typedef struct MusicPadShmHeader {
    // --- Identification & Versioning (offset 0..15) ---
    uint32_t magic;                     // offset 0:   MPAD_SHM_MAGIC (0x4441504D)
    uint32_t protocol_version;          // offset 4:   MPAD_SHM_VERSION (1)
    uint32_t apo_build_version;         // offset 8:   MPAD_APO_BUILD_VERSION written by APO
    uint32_t active_flags;              // offset 12:  MPAD_FLAG_* bitmask

    // --- Writer Lease & Heartbeats (offset 16..39) ---
    uint32_t writer_pid;                // offset 16:  PID of active MusicPad process
    uint32_t flush_seq;                 // offset 20:  Incremented by app to flush ring buffer immediately
    int64_t  app_heartbeat_ns;          // offset 24:  Monotonic QPC timestamp (ns) written by app
    int64_t  apo_heartbeat_ns;          // offset 32:  Monotonic QPC timestamp (ns) written by APO

    // --- Negotiated Endpoint Format & Voice Controls (offset 40..71) ---
    uint32_t apo_sample_rate;           // offset 40:  Negotiated endpoint sample rate (e.g. 48000)
    uint32_t apo_channels;              // offset 44:  Negotiated endpoint channel count (e.g. 2)
    float    mic_gain;                  // offset 48:  Voice gain multiplier (0.0 .. 4.0, default 1.0)
    uint32_t voice_fx_flags;            // offset 52:  MPAD_FX_* bitmask
    float    voice_fx_param1;           // offset 56:  Reserved parameter 1 for voice FX
    float    voice_fx_param2;           // offset 60:  Reserved parameter 2 for voice FX
    uint64_t underrun_count;            // offset 64:  Incremented by APO when ring buffer underruns

    // --- Target Endpoint GUID (offset 72..151) ---
    uint16_t target_endpoint_guid[40];  // offset 72:  UTF-16LE GUID string e.g. "{547a2ef2-...}" (80 bytes)

    // --- Ring Buffer Metadata (offset 152..191) ---
    uint32_t capacity_samples;          // offset 152: MPAD_RING_CAPACITY_SAMPLES (96000)
    uint32_t reserved_pad0;             // offset 156: alignment padding
    uint64_t write_sample_idx;          // offset 160: Monotonic sample write index (owned by app)
    uint64_t read_sample_idx;           // offset 168: Monotonic sample read index (owned by APO)
    uint64_t apo_processed_blocks;      // offset 176: Diagnostic counter of blocks processed by APO
    uint64_t reserved_pad1;             // offset 184: Reserved

    // --- Reserved Future Expansion (offset 192..511) ---
    uint8_t  reserved[320];             // offset 192..511 -> total header = 512 bytes
} MusicPadShmHeader;

typedef struct MusicPadShmLayout {
    MusicPadShmHeader header;
    float             samples[MPAD_RING_CAPACITY_SAMPLES];
} MusicPadShmLayout;
#pragma pack(pop)

#ifdef __cplusplus
static_assert(sizeof(MusicPadShmHeader) == MPAD_HEADER_SIZE_BYTES, "MusicPadShmHeader must be 512 bytes");
static_assert(offsetof(MusicPadShmHeader, magic) == 0, "offset magic");
static_assert(offsetof(MusicPadShmHeader, protocol_version) == 4, "offset protocol_version");
static_assert(offsetof(MusicPadShmHeader, apo_build_version) == 8, "offset apo_build_version");
static_assert(offsetof(MusicPadShmHeader, active_flags) == 12, "offset active_flags");
static_assert(offsetof(MusicPadShmHeader, writer_pid) == 16, "offset writer_pid");
static_assert(offsetof(MusicPadShmHeader, flush_seq) == 20, "offset flush_seq");
static_assert(offsetof(MusicPadShmHeader, app_heartbeat_ns) == 24, "offset app_heartbeat_ns");
static_assert(offsetof(MusicPadShmHeader, apo_heartbeat_ns) == 32, "offset apo_heartbeat_ns");
static_assert(offsetof(MusicPadShmHeader, apo_sample_rate) == 40, "offset apo_sample_rate");
static_assert(offsetof(MusicPadShmHeader, apo_channels) == 44, "offset apo_channels");
static_assert(offsetof(MusicPadShmHeader, mic_gain) == 48, "offset mic_gain");
static_assert(offsetof(MusicPadShmHeader, voice_fx_flags) == 52, "offset voice_fx_flags");
static_assert(offsetof(MusicPadShmHeader, underrun_count) == 64, "offset underrun_count");
static_assert(offsetof(MusicPadShmHeader, target_endpoint_guid) == 72, "offset target_endpoint_guid");
static_assert(offsetof(MusicPadShmHeader, capacity_samples) == 152, "offset capacity_samples");
static_assert(offsetof(MusicPadShmHeader, write_sample_idx) == 160, "offset write_sample_idx");
static_assert(offsetof(MusicPadShmHeader, read_sample_idx) == 168, "offset read_sample_idx");
static_assert(offsetof(MusicPadShmHeader, apo_processed_blocks) == 176, "offset apo_processed_blocks");
static_assert(sizeof(MusicPadShmLayout) == MPAD_SHM_TOTAL_BYTES, "MusicPadShmLayout total size mismatch");
#endif

#ifdef __cplusplus
}
#endif

#endif // MUSICPAD_SHM_H
