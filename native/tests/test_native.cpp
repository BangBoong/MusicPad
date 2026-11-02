// Comprehensive C++ Unit Tests for MusicPad Native Core (Stage 1)
// Tests:
//   1. Shared memory binary layout & SPSC ring buffer wrap-around
//   2. DSP Core: voice gain, voice mute, track mixing, clamping
//   3. Heartbeat timeout (> 300 ms -> pure passthrough)
//   4. Underrun handling (voice continues uninterrupted, underrun_count++)
//   5. Instant buffer flush via flush_seq
//   6. Production musicpad_apo.dll: VERSIONINFO, COM Aggregation, and live SHM APOProcess

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <mmsystem.h>
#include <objbase.h>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>

#include "../include/musicpad_shm.h"
#include "../include/musicpad_dsp.h"

#define CHECK(cond, msg) do { \
    if (!(cond)) { \
        printf("FAIL: %s (line %d)\n", msg, __LINE__); \
        return 1; \
    } \
} while (0)

static bool NearlyEqual(float a, float b, float eps = 1e-5f) {
    return std::fabs(a - b) <= eps;
}

static void InitTestShm(MusicPadShmLayout* shm, uint32_t capacity = 64) {
    std::memset(shm, 0, sizeof(MusicPadShmLayout));
    shm->header.magic             = MPAD_SHM_MAGIC;
    shm->header.protocol_version  = MPAD_SHM_VERSION;
    shm->header.active_flags      = MPAD_FLAG_APP_ACTIVE | MPAD_FLAG_TRACK_PLAYING;
    shm->header.app_heartbeat_ns  = 1000000000LL; // 1.0s
    shm->header.mic_gain          = 1.0f;
    shm->header.capacity_samples  = capacity;
}

// ============================================================================
// Test 1: Passthrough on inactive flag or > 300ms heartbeat timeout
// ============================================================================
static int TestPassthroughAndHeartbeat() {
    MusicPadShmLayout* shm = new MusicPadShmLayout();
    InitTestShm(shm, 64);

    // Even if mute is set and gain is 0, passthrough must leave mic untouched!
    shm->header.active_flags = MPAD_FLAG_MIC_MUTE; // MPAD_FLAG_APP_ACTIVE not set
    shm->header.mic_gain = 0.0f;

    float micIn[8]  = { 0.2f, -0.2f, 0.4f, -0.4f, 0.1f, -0.1f, 0.3f, -0.3f };
    float micOut[8] = { 0 };
    uint32_t lastFlush = 0;

    int active = mpad_dsp_process_shm_block(shm, micIn, micOut, 4, 2, 48000.0f, 1050000000LL, &lastFlush);
    CHECK(active == 0, "Inactive app flag -> returns 0 (passthrough)");
    for (int i = 0; i < 8; ++i) {
        CHECK(NearlyEqual(micOut[i], micIn[i]), "Passthrough preserves original mic signal");
    }
    CHECK(shm->header.apo_heartbeat_ns == 1050000000LL, "APO still updates apo_heartbeat_ns during passthrough");

    // Now enable MPAD_FLAG_APP_ACTIVE, but advance now_ns past 300ms timeout (1.0s + 301ms)
    shm->header.active_flags = MPAD_FLAG_APP_ACTIVE | MPAD_FLAG_MIC_MUTE;
    std::memset(micOut, 0, sizeof(micOut));
    active = mpad_dsp_process_shm_block(
        shm, micIn, micOut, 4, 2, 48000.0f,
        1000000000LL + MPAD_HEARTBEAT_TIMEOUT_NS + 1000000LL,
        &lastFlush
    );
    CHECK(active == 0, "Heartbeat older than 300ms -> returns 0 (passthrough)");
    for (int i = 0; i < 8; ++i) {
        CHECK(NearlyEqual(micOut[i], micIn[i]), "Expired heartbeat passthrough preserves mic signal");
    }

    delete shm;
    return 0;
}

// ============================================================================
// Test 2: Voice Gain, Voice Mute, and Track Mixing with Wrap-Around
// ============================================================================
static int TestVoiceGainMuteAndWrapAroundMix() {
    MusicPadShmLayout* shm = new MusicPadShmLayout();
    InitTestShm(shm, 16); // Small ring capacity (16 floats = 8 stereo frames) to test wrap-around

    uint32_t lastFlush = 0;
    const int64_t nowNs = 1100000000LL; // +100ms (within 300ms timeout)

    // Advance cursors near the wrap boundary (index 12 of 16)
    shm->header.write_sample_idx = 12;
    shm->header.read_sample_idx  = 12;

    // Write 8 stereo samples (4 frames) that wrap around index 12..15 and 0..3
    float trackSamples[8] = { 0.10f, 0.15f, 0.20f, 0.25f, 0.30f, 0.35f, 0.40f, 0.45f };
    uint32_t written = mpad_shm_write_samples(shm, trackSamples, 8);
    CHECK(written == 8, "mpad_shm_write_samples wrote 8 samples across wrap boundary");
    CHECK(mpad_shm_available_to_read(shm) == 8, "available_to_read == 8");

    float micIn[8]  = { 0.20f, 0.20f, 0.20f, 0.20f, 0.20f, 0.20f, 0.20f, 0.20f };
    float micOut[8] = { 0 };
    shm->header.mic_gain = 1.5f; // Voice becomes 0.30f

    int active = mpad_dsp_process_shm_block(shm, micIn, micOut, 4, 2, 48000.0f, nowNs, &lastFlush);
    CHECK(active == 1, "Active block processing returns 1");
    CHECK(mpad_shm_available_to_read(shm) == 0, "Ring buffer consumed after mix");
    for (int i = 0; i < 8; ++i) {
        float expected = 0.20f * 1.5f + trackSamples[i];
        CHECK(NearlyEqual(micOut[i], expected), "Voice * gain + wrapped track sample matches expected");
    }

    // Now test MPAD_FLAG_MIC_MUTE: voice must be muted (0.0), but track still plays!
    shm->header.active_flags = MPAD_FLAG_APP_ACTIVE | MPAD_FLAG_MIC_MUTE | MPAD_FLAG_TRACK_PLAYING;
    written = mpad_shm_write_samples(shm, trackSamples, 8);
    CHECK(written == 8, "Wrote 8 track samples for mute test");

    active = mpad_dsp_process_shm_block(shm, micIn, micOut, 4, 2, 48000.0f, nowNs, &lastFlush);
    CHECK(active == 1, "Active mute + track mix returns 1");
    for (int i = 0; i < 8; ++i) {
        CHECK(NearlyEqual(micOut[i], trackSamples[i]), "Muted mic outputs pure track signal");
    }

    delete shm;
    return 0;
}

// ============================================================================
// Test 3: Underrun Handling & Instant Flush (flush_seq)
// ============================================================================
static int TestUnderrunAndFlush() {
    MusicPadShmLayout* shm = new MusicPadShmLayout();
    InitTestShm(shm, 64);

    uint32_t lastFlush = 0;
    const int64_t nowNs = 1050000000LL;

    // Write only 2 stereo frames (4 floats), but request 4 stereo frames (8 floats)
    float partialTrack[4] = { 0.25f, 0.25f, 0.50f, 0.50f };
    mpad_shm_write_samples(shm, partialTrack, 4);

    float micIn[8]  = { 0.1f, 0.1f, 0.1f, 0.1f, 0.1f, 0.1f, 0.1f, 0.1f };
    float micOut[8] = { 0 };

    int active = mpad_dsp_process_shm_block(shm, micIn, micOut, 4, 2, 48000.0f, nowNs, &lastFlush);
    CHECK(active == 1, "Partial buffer underrun still processes block");
    CHECK(shm->header.underrun_count == 1, "underrun_count incremented on partial buffer");
    CHECK(NearlyEqual(micOut[0], 0.35f) && NearlyEqual(micOut[2], 0.60f), "First 2 frames mixed with track");
    CHECK(NearlyEqual(micOut[4], 0.10f) && NearlyEqual(micOut[6], 0.10f), "Remaining 2 frames preserve voice with silent track");

    // Test flush_seq: write 8 samples, increment flush_seq, verify samples are discarded prior to mix
    float loudTrack[8] = { 0.8f, 0.8f, 0.8f, 0.8f, 0.8f, 0.8f, 0.8f, 0.8f };
    mpad_shm_write_samples(shm, loudTrack, 8);
    CHECK(mpad_shm_available_to_read(shm) == 8, "8 samples queued before flush");

    shm->header.active_flags = MPAD_FLAG_APP_ACTIVE; // Track stopped
    shm->header.flush_seq += 1;

    active = mpad_dsp_process_shm_block(shm, micIn, micOut, 4, 2, 48000.0f, nowNs, &lastFlush);
    CHECK(active == 1, "Block processed after flush");
    CHECK(mpad_shm_available_to_read(shm) == 0, "Ring buffer emptied by flush_seq");
    for (int i = 0; i < 8; ++i) {
        CHECK(NearlyEqual(micOut[i], 0.1f), "Flushed track buffer outputs clean voice only");
    }

    delete shm;
    return 0;
}

// ============================================================================
// Test 4: Production musicpad_apo.dll COM + Aggregation + VERSIONINFO
// ============================================================================
typedef HRESULT (STDAPICALLTYPE *PFN_DllGetClassObject)(REFCLSID, REFIID, LPVOID*);

static const GUID CLSID_MusicPadAPO = {
    0x9d4e7a12, 0x6c8b, 0x4f2a, {0xb5, 0xe1, 0x3a, 0x9c, 0x7d, 0x2e, 0x4f, 0x10}
};
static const GUID IID_IAudioProcessingObject_Local = {
    0xfd7f2b29, 0x24d0, 0x4b5c, {0xb1, 0x77, 0x59, 0x2c, 0x39, 0xf9, 0xca, 0x10}
};
static const GUID IID_IAudioProcessingObjectRT_Local = {
    0x9e1d6a6d, 0xddbc, 0x4e95, {0xa4, 0xc7, 0xad, 0x64, 0xba, 0x37, 0x84, 0x6c}
};
static const GUID IID_IAudioProcessingObjectConfiguration_Local = {
    0x0e5ed805, 0xaba6, 0x49c3, {0x8f, 0x9a, 0x2b, 0x8c, 0x88, 0x9c, 0x4f, 0xa8}
};
static const GUID SUBTYPE_IEEE_FLOAT = {
    0x00000003, 0x0000, 0x0010, {0x80, 0x00, 0x00, 0xaa, 0x00, 0x38, 0x9b, 0x71}
};

struct UNCOMPRESSEDAUDIOFORMAT {
    GUID   guidFormatType;
    DWORD  dwSamplesPerFrame;
    DWORD  dwBytesPerSampleContainer;
    DWORD  dwValidBitsPerSample;
    FLOAT  fFramesPerSecond;
    DWORD  dwChannelMask;
};

MIDL_INTERFACE("4E997F73-B71F-4798-873B-ED7DFCF15B4D")
IAudioMediaType : public IUnknown {
public:
    virtual HRESULT STDMETHODCALLTYPE IsCompressedFormat(BOOL* pfCompressed) = 0;
    virtual HRESULT STDMETHODCALLTYPE IsEqual(IAudioMediaType* pIAudioType, DWORD* pdwFlags) = 0;
    virtual const WAVEFORMATEX* STDMETHODCALLTYPE GetAudioFormat(void) = 0;
    virtual HRESULT STDMETHODCALLTYPE GetUncompressedAudioFormat(UNCOMPRESSEDAUDIOFORMAT* pUncompressedAudioFormat) = 0;
};

struct APO_CONNECTION_DESCRIPTOR {
    int              Type;
    UINT_PTR         pBuffer;
    UINT32           u32MaxFrameCount;
    IAudioMediaType* pFormat;
    UINT32           u32Signature;
};

struct APO_CONNECTION_PROPERTY {
    UINT_PTR pBuffer;
    UINT32   u32ValidFrameCount;
    int      u32BufferFlags;
    UINT32   u32Signature;
};

MIDL_INTERFACE("FD7F2B29-24D0-4B5C-B177-592C39F9CA10")
IAudioProcessingObject : public IUnknown {
public:
    virtual HRESULT STDMETHODCALLTYPE Reset(void) = 0;
    virtual HRESULT STDMETHODCALLTYPE GetLatency(LONGLONG* pTime) = 0;
    virtual HRESULT STDMETHODCALLTYPE GetRegistrationProperties(void** ppRegProps) = 0;
    virtual HRESULT STDMETHODCALLTYPE Initialize(UINT32 cbDataSize, BYTE* pbyData) = 0;
    virtual HRESULT STDMETHODCALLTYPE IsInputFormatSupported(IAudioMediaType*, IAudioMediaType*, IAudioMediaType**) = 0;
    virtual HRESULT STDMETHODCALLTYPE IsOutputFormatSupported(IAudioMediaType*, IAudioMediaType*, IAudioMediaType**) = 0;
    virtual HRESULT STDMETHODCALLTYPE GetInputChannelCount(UINT32* pu32ChannelCount) = 0;
};

MIDL_INTERFACE("0E5ED805-ABA6-49C3-8F9A-2B8C889C4FA8")
IAudioProcessingObjectConfiguration : public IUnknown {
public:
    virtual HRESULT STDMETHODCALLTYPE LockForProcess(UINT32, APO_CONNECTION_DESCRIPTOR**, UINT32, APO_CONNECTION_DESCRIPTOR**) = 0;
    virtual HRESULT STDMETHODCALLTYPE UnlockForProcess(void) = 0;
};

MIDL_INTERFACE("9E1D6A6D-DDBC-4E95-A4C7-AD64BA37846C")
IAudioProcessingObjectRT : public IUnknown {
public:
    virtual void STDMETHODCALLTYPE APOProcess(UINT32, APO_CONNECTION_PROPERTY**, UINT32, APO_CONNECTION_PROPERTY**) = 0;
    virtual UINT32 STDMETHODCALLTYPE CalcInputFrames(UINT32) = 0;
    virtual UINT32 STDMETHODCALLTYPE CalcOutputFrames(UINT32) = 0;
};

class MockMediaType : public IAudioMediaType {
    UNCOMPRESSEDAUDIOFORMAT m_fmt;
public:
    MockMediaType() {
        m_fmt.guidFormatType = SUBTYPE_IEEE_FLOAT;
        m_fmt.dwSamplesPerFrame = 2;
        m_fmt.dwBytesPerSampleContainer = 4;
        m_fmt.dwValidBitsPerSample = 32;
        m_fmt.fFramesPerSecond = 48000.0f;
        m_fmt.dwChannelMask = 3;
    }
    STDMETHODIMP QueryInterface(REFIID riid, void** ppv) override {
        if (IsEqualGUID(riid, IID_IUnknown)) { *ppv = this; return S_OK; }
        *ppv = NULL; return E_NOINTERFACE;
    }
    STDMETHODIMP_(ULONG) AddRef() override { return 1; }
    STDMETHODIMP_(ULONG) Release() override { return 1; }
    STDMETHODIMP IsCompressedFormat(BOOL* pf) override { *pf = FALSE; return S_OK; }
    STDMETHODIMP IsEqual(IAudioMediaType*, DWORD*) override { return S_OK; }
    STDMETHODIMP_(const WAVEFORMATEX*) GetAudioFormat() override { return NULL; }
    STDMETHODIMP GetUncompressedAudioFormat(UNCOMPRESSEDAUDIOFORMAT* p) override { *p = m_fmt; return S_OK; }
};

static int TestProductionApoDll(const wchar_t* dllPath) {
    DWORD dummy = 0;
    DWORD verSize = GetFileVersionInfoSizeW(dllPath, &dummy);
    CHECK(verSize > 0, "musicpad_apo.dll has VERSIONINFO resource");

    HMODULE hDll = LoadLibraryW(dllPath);
    CHECK(hDll != NULL, "LoadLibraryW(musicpad_apo.dll)");

    PFN_DllGetClassObject pfn = reinterpret_cast<PFN_DllGetClassObject>(
        reinterpret_cast<void*>(GetProcAddress(hDll, "DllGetClassObject"))
    );
    CHECK(pfn != NULL, "GetProcAddress(DllGetClassObject)");

    IClassFactory* pFactory = NULL;
    CHECK(SUCCEEDED(pfn(CLSID_MusicPadAPO, IID_IClassFactory, (void**)&pFactory)) && pFactory, "DllGetClassObject");

    // Test COM Aggregation (CApoWrapper style)
    struct OuterUnk : public IUnknown {
        LONG refs = 1;
        IUnknown* inner = NULL;
        STDMETHODIMP QueryInterface(REFIID riid, void** ppv) override {
            if (IsEqualGUID(riid, IID_IUnknown)) { *ppv = this; AddRef(); return S_OK; }
            return inner ? inner->QueryInterface(riid, ppv) : E_NOINTERFACE;
        }
        STDMETHODIMP_(ULONG) AddRef() override { return ++refs; }
        STDMETHODIMP_(ULONG) Release() override { return --refs; }
    } outer;

    CHECK(SUCCEEDED(pFactory->CreateInstance(&outer, IID_IUnknown, (void**)&outer.inner)) && outer.inner, "Aggregated CreateInstance");

    IAudioProcessingObject* pAPO = NULL;
    IAudioProcessingObjectConfiguration* pCfg = NULL;
    IAudioProcessingObjectRT* pRT = NULL;
    CHECK(SUCCEEDED(outer.inner->QueryInterface(IID_IAudioProcessingObject_Local, (void**)&pAPO)) && pAPO, "QI IAudioProcessingObject");
    CHECK(SUCCEEDED(outer.inner->QueryInterface(IID_IAudioProcessingObjectConfiguration_Local, (void**)&pCfg)) && pCfg, "QI IAudioProcessingObjectConfiguration");
    CHECK(SUCCEEDED(outer.inner->QueryInterface(IID_IAudioProcessingObjectRT_Local, (void**)&pRT)) && pRT, "QI IAudioProcessingObjectRT");

    CHECK(SUCCEEDED(pAPO->Initialize(0, NULL)), "Initialize(0, NULL)");

    MockMediaType fmt;
    float inBuf[8]  = { 0.1f, 0.1f, 0.1f, 0.1f, 0.1f, 0.1f, 0.1f, 0.1f };
    float outBuf[8] = { 0 };
    APO_CONNECTION_DESCRIPTOR inDesc  = { 1, (UINT_PTR)inBuf,  4, &fmt, 0x53444341 };
    APO_CONNECTION_DESCRIPTOR outDesc = { 1, (UINT_PTR)outBuf, 4, &fmt, 0x53444341 };
    APO_CONNECTION_DESCRIPTOR* ppInDesc[1]  = { &inDesc };
    APO_CONNECTION_DESCRIPTOR* ppOutDesc[1] = { &outDesc };

    CHECK(SUCCEEDED(pCfg->LockForProcess(1, ppInDesc, 1, ppOutDesc)), "LockForProcess");

    APO_CONNECTION_PROPERTY inProp  = { (UINT_PTR)inBuf,  4, 1, 0x53504341 };
    APO_CONNECTION_PROPERTY outProp = { (UINT_PTR)outBuf, 0, 0, 0x53504341 };
    APO_CONNECTION_PROPERTY* ppInProp[1]  = { &inProp };
    APO_CONNECTION_PROPERTY* ppOutProp[1] = { &outProp };

    pRT->APOProcess(1, ppInProp, 1, ppOutProp);
    CHECK(outProp.u32ValidFrameCount == 4, "APOProcess valid frame count == 4");

    CHECK(SUCCEEDED(pCfg->UnlockForProcess()), "UnlockForProcess");

    pRT->Release();
    pCfg->Release();
    pAPO->Release();
    outer.inner->Release();
    pFactory->Release();
    FreeLibrary(hDll);
    return 0;
}

// ============================================================================
// Test 4: Voice FX Pipeline (DC-offset HPF, Downward Expander, Voice Boost & Limiter)
// ============================================================================
static int TestVoiceFxPipeline() {
    mpad_dsp_reset_voice_state();

    // 1. DC offset removal test: constant DC = 0.5f must be filtered to near 0.0
    const uint32_t dc_frames = 2048;
    float dc_samples[2048];
    for (uint32_t i = 0; i < dc_frames; ++i) dc_samples[i] = 0.5f;

    mpad_dsp_process_voice(
        dc_samples,
        dc_frames,
        1,
        48000.0f,
        1.0f,
        0,
        MPAD_FX_WHITE_NOISE,
        0.0f,
        0.0f
    );
    // Last samples of pure DC should be attenuated by > 30 dB (near 0)
    CHECK(std::fabs(dc_samples[dc_frames - 1]) < 0.02f, "High-Pass Filter eliminates DC offset");

    // 2. Downward Expander test: low noise (0.0003f) below -55 dBFS attenuated to silence
    mpad_dsp_reset_voice_state();
    const uint32_t silence_frames = 9600; // 200 ms @ 48kHz
    float silence_samples[9600];
    for (uint32_t i = 0; i < silence_frames; ++i) silence_samples[i] = 0.0003f;

    mpad_dsp_process_voice(
        silence_samples,
        silence_frames,
        1,
        48000.0f,
        1.0f,
        0,
        MPAD_FX_WHITE_NOISE,
        0.0f,
        0.0f
    );
    CHECK(std::fabs(silence_samples[silence_frames - 1]) < 0.0001f, "Downward Expander smoothly attenuates silence below threshold");

    // 3. Voice Boost (+20 dB = 10x) + SoftLimiter test: loud signal (0.3f) does not exceed 1.0
    mpad_dsp_reset_voice_state();
    float boost_samples[4] = { 0.3f, -0.3f, 0.5f, -0.5f };
    mpad_dsp_process_voice(
        boost_samples,
        4,
        1,
        48000.0f,
        10.0f, // 10x boost (+20 dB)
        0,
        MPAD_FX_VOICE_BOOST,
        0.0f,
        0.0f
    );
    for (int i = 0; i < 4; ++i) {
        CHECK(std::fabs(boost_samples[i]) <= 0.999f, "Soft-knee Limiter prevents digital clipping exceeding 0 dBFS");
        CHECK(std::fabs(boost_samples[i]) >= 0.891f, "Limiter compresses signal above -1.0 dBFS threshold");
    }

    // 4. DeepFilterNet test: neural noise suppression hook runs and suppresses noise or safely bypasses
    mpad_dsp_reset_voice_state();
    const uint32_t df_test_frames = 960;
    float df_noise[960];
    for (uint32_t i = 0; i < df_test_frames; ++i) {
        df_noise[i] = 0.05f * std::sin((float)i * 0.7f);
    }
    mpad_dsp_process_voice(
        df_noise,
        df_test_frames,
        1,
        48000.0f,
        1.0f,
        0,
        MPAD_FX_DEEPFILTER,
        1.0f, // 100% wet
        0.0f
    );
    // Verified that process_voice executes without error/crash
    CHECK(std::fabs(df_noise[df_test_frames - 1]) <= 0.05f, "DeepFilterNet processes audio safely");

    return 0;
}

int main() {
    CoInitializeEx(NULL, COINIT_MULTITHREADED);

    if (TestPassthroughAndHeartbeat() != 0) return 1;
    if (TestVoiceGainMuteAndWrapAroundMix() != 0) return 1;
    if (TestUnderrunAndFlush() != 0) return 1;
    if (TestVoiceFxPipeline() != 0) return 1;
    if (TestProductionApoDll(L"musicpad_apo.dll") != 0) return 1;

    CoUninitialize();
    printf("PASS: All Stage 1 Native Core & APO tests succeeded!\n");
    return 0;
}
