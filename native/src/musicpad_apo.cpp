// MusicPad Production EFX Audio Processing Object (musicpad_apo.dll)
// CLSID: {9D4E7A12-6C8B-4F2A-B5E1-3A9C7D2E4F10}
//
// Features:
// - Full COM Aggregation support (INonDelegatingUnknown) required by audiodg.exe CApoWrapper
// - Wraps downstream OriginalApo (vendor EFX or Equalizer APO) if configured
// - Maps C:\ProgramData\MusicPad\injection.bin in Initialize/LockForProcess (non-RT path)
// - Real-time safe APOProcess: zero allocations, zero locks, automatic 300ms passthrough fallback

#ifndef _CRT_SECURE_NO_WARNINGS
#define _CRT_SECURE_NO_WARNINGS
#endif
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <mmsystem.h>
#include <objbase.h>
#include <propidl.h>
#include <cstdio>
#include <cstring>
#include <cwchar>
#include <new>

#include "../include/musicpad_shm.h"
#include "../include/musicpad_dsp.h"

// ============================================================================
// 1. GUIDs & Constants
// ============================================================================

// Production MusicPad APO CLSID: {9D4E7A12-6C8B-4F2A-B5E1-3A9C7D2E4F10}
static const GUID CLSID_MusicPadAPO = {
    0x9d4e7a12, 0x6c8b, 0x4f2a, {0xb5, 0xe1, 0x3a, 0x9c, 0x7d, 0x2e, 0x4f, 0x10}
};

// IID_IAudioProcessingObject: {FD7F2B29-24D0-4B5C-B177-592C39F9CA10}
static const GUID IID_IAudioProcessingObject_Local = {
    0xfd7f2b29, 0x24d0, 0x4b5c, {0xb1, 0x77, 0x59, 0x2c, 0x39, 0xf9, 0xca, 0x10}
};

// IID_IAudioProcessingObjectRT: {9E1D6A6D-DDBC-4E95-A4C7-AD64BA37846C}
static const GUID IID_IAudioProcessingObjectRT_Local = {
    0x9e1d6a6d, 0xddbc, 0x4e95, {0xa4, 0xc7, 0xad, 0x64, 0xba, 0x37, 0x84, 0x6c}
};

// IID_IAudioProcessingObjectConfiguration: {0E5ED805-ABA6-49C3-8F9A-2B8C889C4FA8}
static const GUID IID_IAudioProcessingObjectConfiguration_Local = {
    0x0e5ed805, 0xaba6, 0x49c3, {0x8f, 0x9a, 0x2b, 0x8c, 0x88, 0x9c, 0x4f, 0xa8}
};

// IID_IAudioSystemEffects: {5FA00F27-ADD6-499A-8A9D-6B98521FA75B}
static const GUID IID_IAudioSystemEffects_Local = {
    0x5fa00f27, 0xadd6, 0x499a, {0x8a, 0x9d, 0x6b, 0x98, 0x52, 0x1f, 0xa7, 0x5b}
};

// KSDATAFORMAT_SUBTYPE_IEEE_FLOAT: {00000003-0000-0010-8000-00AA00389B71}
static const GUID SUBTYPE_IEEE_FLOAT = {
    0x00000003, 0x0000, 0x0010, {0x80, 0x00, 0x00, 0xaa, 0x00, 0x38, 0x9b, 0x71}
};

// PKEY_AudioEndpoint_GUID: {1DA5D803-D492-4EDD-8C23-E0C0FFEE7F0E}, pid = 4
static const PROPERTYKEY PKEY_Endpoint_GUID_Local = {
    {0x1da5d803, 0xd492, 0x4edd, {0x8c, 0x23, 0xe0, 0xc0, 0xff, 0xee, 0x7f, 0x0e}},
    4
};

#define APOERR_ALREADY_INITIALIZED               ((HRESULT)0x887D0001L)
#define APOERR_NOT_INITIALIZED                   ((HRESULT)0x887D0002L)
#define APOERR_FORMAT_NOT_SUPPORTED              ((HRESULT)0x887D0003L)
#define APOERR_ALREADY_UNLOCKED                  ((HRESULT)0x887D0006L)
#define APOERR_INVALID_CONNECTION_FORMAT         ((HRESULT)0x887D0009L)
#define APOERR_APO_LOCKED                        ((HRESULT)0x887D000AL)

// ============================================================================
// 2. Binary Struct Layouts & APO COM Interfaces
// ============================================================================

typedef LONGLONG HNSTIME;

enum APO_BUFFER_FLAGS {
    BUFFER_INVALID = 0,
    BUFFER_VALID   = 1,
    BUFFER_SILENT  = 2
};

enum APO_CONNECTION_BUFFER_TYPE {
    APO_CONNECTION_BUFFER_TYPE_ALLOCATED = 0,
    APO_CONNECTION_BUFFER_TYPE_EXTERNAL  = 1,
    APO_CONNECTION_BUFFER_TYPE_DEPENDANT = 2
};

enum APO_FLAG {
    APO_FLAG_NONE                        = 0x00000000,
    APO_FLAG_INPLACE                     = 0x00000001,
    APO_FLAG_SAMPLESPERFRAME_MUST_MATCH  = 0x00000002,
    APO_FLAG_FRAMESPERSECOND_MUST_MATCH  = 0x00000004,
    APO_FLAG_BITSPERSAMPLE_MUST_MATCH    = 0x00000008,
    APO_FLAG_MIXER                       = 0x00000010,
    APO_FLAG_DEFAULT                     = 0x0000000E
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
    APO_CONNECTION_BUFFER_TYPE Type;
    UINT_PTR                   pBuffer;
    UINT32                     u32MaxFrameCount;
    IAudioMediaType*           pFormat;
    UINT32                     u32Signature;
};

struct APO_CONNECTION_PROPERTY {
    UINT_PTR         pBuffer;
    UINT32           u32ValidFrameCount;
    APO_BUFFER_FLAGS u32BufferFlags;
    UINT32           u32Signature;
};

struct APO_REG_PROPERTIES {
    CLSID    clsid;
    APO_FLAG Flags;
    WCHAR    szFriendlyName[256];
    WCHAR    szCopyrightInfo[256];
    UINT32   u32MajorVersion;
    UINT32   u32MinorVersion;
    UINT32   u32MinInputConnections;
    UINT32   u32MaxInputConnections;
    UINT32   u32MinOutputConnections;
    UINT32   u32MaxOutputConnections;
    UINT32   u32MaxInstances;
    UINT32   u32NumAPOInterfaces;
    IID      rgiidAPOInterfaces[1];
};

static_assert(sizeof(APO_REG_PROPERTIES) == 0x444, "APO_REG_PROPERTIES must be 0x444 bytes");

MIDL_INTERFACE("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99")
IPropertyStore_Local : public IUnknown {
public:
    virtual HRESULT STDMETHODCALLTYPE GetCount(DWORD* cProps) = 0;
    virtual HRESULT STDMETHODCALLTYPE GetAt(DWORD iProp, PROPERTYKEY* pkey) = 0;
    virtual HRESULT STDMETHODCALLTYPE GetValue(const PROPERTYKEY& key, PROPVARIANT* pv) = 0;
    virtual HRESULT STDMETHODCALLTYPE SetValue(const PROPERTYKEY& key, REFPROPVARIANT propvar) = 0;
    virtual HRESULT STDMETHODCALLTYPE Commit(void) = 0;
};

struct APOInitBaseStruct {
    UINT32 cbSize;
    CLSID  clsid;
};

struct APOInitSystemEffects {
    APOInitBaseStruct     APOInit;
    IPropertyStore_Local* pAPOEndpointProperties;
    IPropertyStore_Local* pAPOSystemEffectsProperties;
    void*                 pReserved;
    void*                 pDeviceCollection;
};

static_assert(sizeof(APOInitSystemEffects) == 0x38, "APOInitSystemEffects must be 0x38 bytes on x64");

MIDL_INTERFACE("FD7F2B29-24D0-4B5C-B177-592C39F9CA10")
IAudioProcessingObject : public IUnknown {
public:
    virtual HRESULT STDMETHODCALLTYPE Reset(void) = 0;
    virtual HRESULT STDMETHODCALLTYPE GetLatency(HNSTIME* pTime) = 0;
    virtual HRESULT STDMETHODCALLTYPE GetRegistrationProperties(APO_REG_PROPERTIES** ppRegProps) = 0;
    virtual HRESULT STDMETHODCALLTYPE Initialize(UINT32 cbDataSize, BYTE* pbyData) = 0;
    virtual HRESULT STDMETHODCALLTYPE IsInputFormatSupported(
        IAudioMediaType* pOppositeFormat,
        IAudioMediaType* pRequestedInputFormat,
        IAudioMediaType** ppSupportedInputFormat) = 0;
    virtual HRESULT STDMETHODCALLTYPE IsOutputFormatSupported(
        IAudioMediaType* pOppositeFormat,
        IAudioMediaType* pRequestedOutputFormat,
        IAudioMediaType** ppSupportedOutputFormat) = 0;
    virtual HRESULT STDMETHODCALLTYPE GetInputChannelCount(UINT32* pu32ChannelCount) = 0;
};

MIDL_INTERFACE("0E5ED805-ABA6-49C3-8F9A-2B8C889C4FA8")
IAudioProcessingObjectConfiguration : public IUnknown {
public:
    virtual HRESULT STDMETHODCALLTYPE LockForProcess(
        UINT32 u32NumInputConnections,
        APO_CONNECTION_DESCRIPTOR** ppInputConnections,
        UINT32 u32NumOutputConnections,
        APO_CONNECTION_DESCRIPTOR** ppOutputConnections) = 0;
    virtual HRESULT STDMETHODCALLTYPE UnlockForProcess(void) = 0;
};

MIDL_INTERFACE("9E1D6A6D-DDBC-4E95-A4C7-AD64BA37846C")
IAudioProcessingObjectRT : public IUnknown {
public:
    virtual void STDMETHODCALLTYPE APOProcess(
        UINT32 u32NumInputConnections,
        APO_CONNECTION_PROPERTY** ppInputConnections,
        UINT32 u32NumOutputConnections,
        APO_CONNECTION_PROPERTY** ppOutputConnections) = 0;
    virtual UINT32 STDMETHODCALLTYPE CalcInputFrames(UINT32 u32OutputFrameCount) = 0;
    virtual UINT32 STDMETHODCALLTYPE CalcOutputFrames(UINT32 u32InputFrameCount) = 0;
};

MIDL_INTERFACE("5FA00F27-ADD6-499A-8A9D-6B98521FA75B")
IAudioSystemEffects : public IUnknown {
};

struct INonDelegatingUnknown {
    virtual HRESULT STDMETHODCALLTYPE NonDelegatingQueryInterface(REFIID riid, void** ppvObject) = 0;
    virtual ULONG   STDMETHODCALLTYPE NonDelegatingAddRef(void) = 0;
    virtual ULONG   STDMETHODCALLTYPE NonDelegatingRelease(void) = 0;
};

// ============================================================================
// 3. MusicPadAPO Implementation
// ============================================================================

static LONG g_dllRefCount = 0;

static const APO_REG_PROPERTIES g_RegProps = {
    CLSID_MusicPadAPO,
    APO_FLAG_DEFAULT,
    L"MusicPad Audio Processing Object",
    L"Copyright (c) 2026 MusicPad",
    1,
    0,
    1,
    1,
    1,
    1,
    0xFFFFFFFF,
    1,
    { IID_IAudioProcessingObject_Local }
};

class MusicPadAPO final :
    public INonDelegatingUnknown,
    public IAudioProcessingObject,
    public IAudioProcessingObjectConfiguration,
    public IAudioProcessingObjectRT,
    public IAudioSystemEffects
{
private:
    LONG                m_refCount;
    IUnknown*           m_pUnkOuter;
    bool                m_initialized;
    bool                m_locked;
    UINT32              m_channels;
    float               m_sampleRate;
    uint32_t            m_lastFlushSeq;
    LARGE_INTEGER       m_qpcFreq;
    WCHAR               m_endpointGuid[64];

    // Shared memory mapping (opened in Initialize/LockForProcess, closed in destructor)
    HANDLE              m_hMapFile;
    HANDLE              m_hMapping;
    MusicPadShmLayout*  m_shm;

    // Wrapped downstream EFX APO (if any)
    IAudioProcessingObject*              m_childAPO;
    IAudioProcessingObjectConfiguration* m_childCfg;
    IAudioProcessingObjectRT*            m_childRT;

    int64_t GetNowNanoseconds() const {
        LARGE_INTEGER counter;
        QueryPerformanceCounter(&counter);
        if (m_qpcFreq.QuadPart <= 0) {
            return 0;
        }
        // Avoid 64-bit overflow while maintaining nanosecond resolution
        const int64_t seconds = counter.QuadPart / m_qpcFreq.QuadPart;
        const int64_t rem     = counter.QuadPart % m_qpcFreq.QuadPart;
        return seconds * 1000000000LL + (rem * 1000000000LL) / m_qpcFreq.QuadPart;
    }

    void CloseShm() {
        if (m_shm) {
            UnmapViewOfFile(m_shm);
            m_shm = NULL;
        }
        if (m_hMapping) {
            CloseHandle(m_hMapping);
            m_hMapping = NULL;
        }
        if (m_hMapFile != INVALID_HANDLE_VALUE) {
            CloseHandle(m_hMapFile);
            m_hMapFile = INVALID_HANDLE_VALUE;
        }
    }

    void OpenAndMapShm() {
        if (m_shm) return;

        CreateDirectoryW(MPAD_DATA_DIR_W, NULL);
        HANDLE hFile = CreateFileW(
            MPAD_SHM_PATH_W,
            GENERIC_READ | GENERIC_WRITE,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            NULL,
            OPEN_ALWAYS,
            FILE_ATTRIBUTE_NORMAL,
            NULL
        );
        if (hFile == INVALID_HANDLE_VALUE) {
            return;
        }

        LARGE_INTEGER fileSize = {};
        if (!GetFileSizeEx(hFile, &fileSize) || fileSize.QuadPart < (LONGLONG)MPAD_SHM_TOTAL_BYTES) {
            LARGE_INTEGER targetSize;
            targetSize.QuadPart = (LONGLONG)MPAD_SHM_TOTAL_BYTES;
            if (!SetFilePointerEx(hFile, targetSize, NULL, FILE_BEGIN) || !SetEndOfFile(hFile)) {
                CloseHandle(hFile);
                return;
            }
        }

        HANDLE hMap = CreateFileMappingW(
            hFile,
            NULL,
            PAGE_READWRITE,
            0,
            (DWORD)MPAD_SHM_TOTAL_BYTES,
            NULL
        );
        if (!hMap) {
            CloseHandle(hFile);
            return;
        }

        MusicPadShmLayout* pLayout = reinterpret_cast<MusicPadShmLayout*>(
            MapViewOfFile(hMap, FILE_MAP_ALL_ACCESS, 0, 0, MPAD_SHM_TOTAL_BYTES)
        );
        if (!pLayout) {
            CloseHandle(hMap);
            CloseHandle(hFile);
            return;
        }

        m_hMapFile = hFile;
        m_hMapping = hMap;
        m_shm      = pLayout;

        // Initialize header defaults if freshly created
        if (m_shm->header.magic != MPAD_SHM_MAGIC) {
            std::memset(&m_shm->header, 0, sizeof(MusicPadShmHeader));
            m_shm->header.magic             = MPAD_SHM_MAGIC;
            m_shm->header.protocol_version  = MPAD_SHM_VERSION;
            m_shm->header.apo_build_version = MPAD_APO_BUILD_VERSION;
            m_shm->header.mic_gain          = 1.0f;
            m_shm->header.capacity_samples  = MPAD_RING_CAPACITY_SAMPLES;
        } else {
            m_shm->header.apo_build_version = MPAD_APO_BUILD_VERSION;
            if (m_shm->header.capacity_samples == 0 || m_shm->header.capacity_samples > MPAD_RING_CAPACITY_SAMPLES) {
                m_shm->header.capacity_samples = MPAD_RING_CAPACITY_SAMPLES;
            }
        }
        m_lastFlushSeq = m_shm->header.flush_seq;
    }

    void ReleaseChildAPO() {
        if (m_childRT)  { m_childRT->Release();  m_childRT = NULL; }
        if (m_childCfg) { m_childCfg->Release(); m_childCfg = NULL; }
        if (m_childAPO) { m_childAPO->Release(); m_childAPO = NULL; }
    }

    void TryLoadChildAPO(UINT32 cbDataSize, BYTE* pbyData) {
        if (!m_endpointGuid[0]) return;

        WCHAR subKey[256];
        _snwprintf(subKey, 255, L"SOFTWARE\\MusicPad\\APO\\%ls", m_endpointGuid);
        subKey[255] = L'\0';

        WCHAR childClsidStr[64] = {0};
        DWORD cbSize = sizeof(childClsidStr);
        DWORD dwType = 0;
        if (RegGetValueW(HKEY_LOCAL_MACHINE, subKey, L"OriginalApo", RRF_RT_REG_SZ, &dwType, childClsidStr, &cbSize) != ERROR_SUCCESS) {
            return;
        }
        if (!childClsidStr[0]) return;

        CLSID childClsid;
        if (FAILED(CLSIDFromString(childClsidStr, &childClsid))) {
            return;
        }
        if (IsEqualGUID(childClsid, CLSID_MusicPadAPO)) {
            return;
        }

        IAudioProcessingObject* pAPO = NULL;
        HRESULT hr = CoCreateInstance(childClsid, NULL, CLSCTX_INPROC_SERVER, IID_IAudioProcessingObject_Local, (void**)&pAPO);
        if (FAILED(hr) || !pAPO) {
            return;
        }

        IAudioProcessingObjectConfiguration* pCfg = NULL;
        IAudioProcessingObjectRT* pRT = NULL;
        if (FAILED(pAPO->QueryInterface(IID_IAudioProcessingObjectConfiguration_Local, (void**)&pCfg)) ||
            FAILED(pAPO->QueryInterface(IID_IAudioProcessingObjectRT_Local, (void**)&pRT))) {
            if (pCfg) pCfg->Release();
            if (pRT)  pRT->Release();
            pAPO->Release();
            return;
        }

        if (FAILED(pAPO->Initialize(cbDataSize, pbyData))) {
            pRT->Release();
            pCfg->Release();
            pAPO->Release();
            return;
        }

        m_childAPO = pAPO;
        m_childCfg = pCfg;
        m_childRT  = pRT;
    }

    bool MatchesTargetEndpoint() const {
        if (!m_shm) return false;
        const wchar_t* shmGuid = reinterpret_cast<const wchar_t*>(m_shm->header.target_endpoint_guid);
        if (shmGuid[0] == L'\0' || m_endpointGuid[0] == L'\0') {
            return true;
        }
        return _wcsicmp(shmGuid, m_endpointGuid) == 0;
    }

    static HRESULT CheckFormat(
        IAudioMediaType* pOppositeFormat,
        IAudioMediaType* pRequestedFormat,
        IAudioMediaType** ppSupportedFormat)
    {
        if (!pRequestedFormat || !ppSupportedFormat) {
            return E_POINTER;
        }
        *ppSupportedFormat = NULL;

        UNCOMPRESSEDAUDIOFORMAT reqFmt = {};
        HRESULT hr = pRequestedFormat->GetUncompressedAudioFormat(&reqFmt);
        if (FAILED(hr)) {
            return hr;
        }

        if (!IsEqualGUID(reqFmt.guidFormatType, SUBTYPE_IEEE_FLOAT) ||
            reqFmt.dwBytesPerSampleContainer != 4 ||
            reqFmt.dwValidBitsPerSample != 32 ||
            reqFmt.dwSamplesPerFrame == 0 ||
            reqFmt.dwSamplesPerFrame > 8 ||
            reqFmt.fFramesPerSecond < 8000.0f ||
            reqFmt.fFramesPerSecond > 192000.0f)
        {
            return APOERR_FORMAT_NOT_SUPPORTED;
        }

        if (pOppositeFormat) {
            UNCOMPRESSEDAUDIOFORMAT oppFmt = {};
            if (SUCCEEDED(pOppositeFormat->GetUncompressedAudioFormat(&oppFmt))) {
                if (!IsEqualGUID(oppFmt.guidFormatType, SUBTYPE_IEEE_FLOAT) ||
                    oppFmt.dwBytesPerSampleContainer != 4 ||
                    oppFmt.dwValidBitsPerSample != 32 ||
                    oppFmt.dwSamplesPerFrame != reqFmt.dwSamplesPerFrame ||
                    oppFmt.fFramesPerSecond != reqFmt.fFramesPerSecond)
                {
                    return APOERR_FORMAT_NOT_SUPPORTED;
                }
            }
        }

        *ppSupportedFormat = pRequestedFormat;
        pRequestedFormat->AddRef();
        return S_OK;
    }

public:
    explicit MusicPadAPO(IUnknown* pUnkOuter)
        : m_refCount(1)
        , m_pUnkOuter(pUnkOuter ? pUnkOuter : reinterpret_cast<IUnknown*>(static_cast<INonDelegatingUnknown*>(this)))
        , m_initialized(false)
        , m_locked(false)
        , m_channels(2)
        , m_sampleRate(48000.0f)
        , m_lastFlushSeq(0)
        , m_hMapFile(INVALID_HANDLE_VALUE)
        , m_hMapping(NULL)
        , m_shm(NULL)
        , m_childAPO(NULL)
        , m_childCfg(NULL)
        , m_childRT(NULL)
    {
        QueryPerformanceFrequency(&m_qpcFreq);
        m_endpointGuid[0] = L'\0';
        InterlockedIncrement(&g_dllRefCount);
    }

    ~MusicPadAPO() {
        ReleaseChildAPO();
        CloseShm();
        InterlockedDecrement(&g_dllRefCount);
    }

    // INonDelegatingUnknown
    STDMETHODIMP NonDelegatingQueryInterface(REFIID riid, void** ppvObject) override {
        if (!ppvObject) return E_POINTER;
        *ppvObject = NULL;

        if (IsEqualGUID(riid, IID_IUnknown)) {
            *ppvObject = reinterpret_cast<void*>(static_cast<INonDelegatingUnknown*>(this));
            NonDelegatingAddRef();
            return S_OK;
        }

        if (IsEqualGUID(riid, IID_IAudioProcessingObject_Local)) {
            *ppvObject = static_cast<IAudioProcessingObject*>(this);
        } else if (IsEqualGUID(riid, IID_IAudioProcessingObjectConfiguration_Local)) {
            *ppvObject = static_cast<IAudioProcessingObjectConfiguration*>(this);
        } else if (IsEqualGUID(riid, IID_IAudioProcessingObjectRT_Local)) {
            *ppvObject = static_cast<IAudioProcessingObjectRT*>(this);
        } else if (IsEqualGUID(riid, IID_IAudioSystemEffects_Local)) {
            *ppvObject = static_cast<IAudioSystemEffects*>(this);
        } else {
            return E_NOINTERFACE;
        }

        m_pUnkOuter->AddRef();
        return S_OK;
    }

    STDMETHODIMP_(ULONG) NonDelegatingAddRef(void) override {
        return (ULONG)InterlockedIncrement(&m_refCount);
    }

    STDMETHODIMP_(ULONG) NonDelegatingRelease(void) override {
        LONG ref = InterlockedDecrement(&m_refCount);
        if (ref == 0) {
            delete this;
        }
        return (ULONG)ref;
    }

    // Delegating IUnknown
    STDMETHODIMP QueryInterface(REFIID riid, void** ppvObject) override {
        return m_pUnkOuter->QueryInterface(riid, ppvObject);
    }

    STDMETHODIMP_(ULONG) AddRef(void) override {
        return m_pUnkOuter->AddRef();
    }

    STDMETHODIMP_(ULONG) Release(void) override {
        return m_pUnkOuter->Release();
    }

    // IAudioProcessingObject
    STDMETHODIMP Reset(void) override {
        if (m_childAPO) {
            m_childAPO->Reset();
        }
        return S_OK;
    }

    STDMETHODIMP GetLatency(HNSTIME* pTime) override {
        if (!pTime) return E_POINTER;
        *pTime = 0;
        if (m_childAPO) {
            HNSTIME childLatency = 0;
            if (SUCCEEDED(m_childAPO->GetLatency(&childLatency))) {
                *pTime = childLatency;
            }
        }
        return S_OK;
    }

    STDMETHODIMP GetRegistrationProperties(APO_REG_PROPERTIES** ppRegProps) override {
        if (!ppRegProps) return E_POINTER;
        *ppRegProps = NULL;

        APO_REG_PROPERTIES* pProps = (APO_REG_PROPERTIES*)CoTaskMemAlloc(sizeof(APO_REG_PROPERTIES));
        if (!pProps) return E_OUTOFMEMORY;

        std::memcpy(pProps, &g_RegProps, sizeof(APO_REG_PROPERTIES));
        *ppRegProps = pProps;
        return S_OK;
    }

    STDMETHODIMP Initialize(UINT32 cbDataSize, BYTE* pbyData) override {
        if (m_initialized) {
            return APOERR_ALREADY_INITIALIZED;
        }
        if ((cbDataSize == 0 && pbyData != NULL) || (cbDataSize > 0 && pbyData == NULL)) {
            return E_INVALIDARG;
        }

        if (cbDataSize >= sizeof(APOInitSystemEffects) && pbyData != NULL) {
            APOInitSystemEffects* pSysFx = reinterpret_cast<APOInitSystemEffects*>(pbyData);
            if (pSysFx->pAPOEndpointProperties != NULL) {
                PROPVARIANT var;
                PropVariantInit(&var);
                if (SUCCEEDED(pSysFx->pAPOEndpointProperties->GetValue(PKEY_Endpoint_GUID_Local, &var))) {
                    if (var.vt == VT_LPWSTR && var.pwszVal != NULL) {
                        wcsncpy(m_endpointGuid, var.pwszVal, 63);
                        m_endpointGuid[63] = L'\0';
                    }
                    PropVariantClear(&var);
                }
            }
        }

        OpenAndMapShm();
        TryLoadChildAPO(cbDataSize, pbyData);

        m_initialized = true;
        return S_OK;
    }

    STDMETHODIMP IsInputFormatSupported(
        IAudioMediaType* pOppositeFormat,
        IAudioMediaType* pRequestedInputFormat,
        IAudioMediaType** ppSupportedInputFormat) override
    {
        return CheckFormat(pOppositeFormat, pRequestedInputFormat, ppSupportedInputFormat);
    }

    STDMETHODIMP IsOutputFormatSupported(
        IAudioMediaType* pOppositeFormat,
        IAudioMediaType* pRequestedOutputFormat,
        IAudioMediaType** ppSupportedOutputFormat) override
    {
        return CheckFormat(pOppositeFormat, pRequestedOutputFormat, ppSupportedOutputFormat);
    }

    STDMETHODIMP GetInputChannelCount(UINT32* pu32ChannelCount) override {
        if (!pu32ChannelCount) return E_POINTER;
        if (!m_locked) return APOERR_NOT_INITIALIZED;
        *pu32ChannelCount = m_channels;
        return S_OK;
    }

    // IAudioProcessingObjectConfiguration
    STDMETHODIMP LockForProcess(
        UINT32 u32NumInputConnections,
        APO_CONNECTION_DESCRIPTOR** ppInputConnections,
        UINT32 u32NumOutputConnections,
        APO_CONNECTION_DESCRIPTOR** ppOutputConnections) override
    {
        if (m_locked) return APOERR_APO_LOCKED;
        if (u32NumInputConnections != 1 || u32NumOutputConnections != 1 ||
            !ppInputConnections || !ppOutputConnections ||
            !ppInputConnections[0] || !ppOutputConnections[0] ||
            !ppInputConnections[0]->pFormat || !ppOutputConnections[0]->pFormat)
        {
            return E_INVALIDARG;
        }

        UNCOMPRESSEDAUDIOFORMAT inFmt = {};
        UNCOMPRESSEDAUDIOFORMAT outFmt = {};
        if (FAILED(ppInputConnections[0]->pFormat->GetUncompressedAudioFormat(&inFmt)) ||
            FAILED(ppOutputConnections[0]->pFormat->GetUncompressedAudioFormat(&outFmt)))
        {
            return APOERR_INVALID_CONNECTION_FORMAT;
        }

        if (!IsEqualGUID(inFmt.guidFormatType, SUBTYPE_IEEE_FLOAT) ||
            inFmt.dwBytesPerSampleContainer != 4 ||
            inFmt.dwValidBitsPerSample != 32 ||
            inFmt.dwSamplesPerFrame != outFmt.dwSamplesPerFrame ||
            inFmt.fFramesPerSecond != outFmt.fFramesPerSecond ||
            inFmt.dwSamplesPerFrame == 0 || inFmt.dwSamplesPerFrame > 8)
        {
            return APOERR_INVALID_CONNECTION_FORMAT;
        }

        if (m_childCfg) {
            if (FAILED(m_childCfg->LockForProcess(u32NumInputConnections, ppInputConnections, u32NumOutputConnections, ppOutputConnections))) {
                ReleaseChildAPO();
            }
        }

        OpenAndMapShm();

        m_channels   = inFmt.dwSamplesPerFrame;
        m_sampleRate = inFmt.fFramesPerSecond;
        m_locked     = true;

        if (m_shm && MatchesTargetEndpoint()) {
            m_shm->header.apo_build_version = MPAD_APO_BUILD_VERSION;
            m_shm->header.apo_sample_rate   = (uint32_t)(m_sampleRate + 0.5f);
            m_shm->header.apo_channels      = m_channels;
            m_shm->header.apo_heartbeat_ns  = GetNowNanoseconds();
        }
        return S_OK;
    }

    STDMETHODIMP UnlockForProcess(void) override {
        if (!m_locked) return APOERR_ALREADY_UNLOCKED;
        if (m_childCfg) {
            m_childCfg->UnlockForProcess();
        }
        m_locked = false;
        return S_OK;
    }

    // IAudioProcessingObjectRT
    STDMETHODIMP_(void) APOProcess(
        UINT32 u32NumInputConnections,
        APO_CONNECTION_PROPERTY** ppInputConnections,
        UINT32 u32NumOutputConnections,
        APO_CONNECTION_PROPERTY** ppOutputConnections) override
    {
        if (!m_locked || u32NumInputConnections == 0 || u32NumOutputConnections == 0 ||
            !ppInputConnections || !ppOutputConnections)
        {
            return;
        }

        APO_CONNECTION_PROPERTY* inConn  = ppInputConnections[0];
        APO_CONNECTION_PROPERTY* outConn = ppOutputConnections[0];
        if (!inConn || !outConn) return;

        const UINT32 frames   = inConn->u32ValidFrameCount;
        const UINT32 channels = m_channels;
        float* inBuf  = reinterpret_cast<float*>(inConn->pBuffer);
        float* outBuf = reinterpret_cast<float*>(outConn->pBuffer);
        if (!inBuf || !outBuf || frames == 0) {
            outConn->u32ValidFrameCount = frames;
            outConn->u32BufferFlags     = inConn->u32BufferFlags;
            return;
        }

        APO_BUFFER_FLAGS currentFlags = inConn->u32BufferFlags;
        if (m_childRT) {
            m_childRT->APOProcess(u32NumInputConnections, ppInputConnections, u32NumOutputConnections, ppOutputConnections);
            currentFlags = outConn->u32BufferFlags;
            if (currentFlags == BUFFER_SILENT) {
                std::memset(outBuf, 0, (size_t)frames * channels * sizeof(float));
            }
        } else {
            if (currentFlags == BUFFER_SILENT) {
                std::memset(outBuf, 0, (size_t)frames * channels * sizeof(float));
            } else if (outBuf != inBuf) {
                std::memcpy(outBuf, inBuf, (size_t)frames * channels * sizeof(float));
            }
        }

        if (m_shm && MatchesTargetEndpoint()) {
            const int64_t now_ns = GetNowNanoseconds();
            const int active = mpad_dsp_process_shm_block(
                m_shm,
                outBuf,
                outBuf,
                frames,
                channels,
                m_sampleRate,
                now_ns,
                &m_lastFlushSeq
            );
            if (active != 0) {
                currentFlags = BUFFER_VALID;
            }
        }

        outConn->u32ValidFrameCount = frames;
        outConn->u32BufferFlags     = (currentFlags == BUFFER_INVALID) ? BUFFER_VALID : currentFlags;
    }

    STDMETHODIMP_(UINT32) CalcInputFrames(UINT32 u32OutputFrameCount) override {
        if (m_childRT) return m_childRT->CalcInputFrames(u32OutputFrameCount);
        return u32OutputFrameCount;
    }

    STDMETHODIMP_(UINT32) CalcOutputFrames(UINT32 u32InputFrameCount) override {
        if (m_childRT) return m_childRT->CalcOutputFrames(u32InputFrameCount);
        return u32InputFrameCount;
    }
};

// ============================================================================
// 4. ClassFactory & DLL Entry Points
// ============================================================================

class MusicPadClassFactory final : public IClassFactory {
private:
    LONG m_refCount;
public:
    MusicPadClassFactory() : m_refCount(1) {
        InterlockedIncrement(&g_dllRefCount);
    }
    ~MusicPadClassFactory() {
        InterlockedDecrement(&g_dllRefCount);
    }

    STDMETHODIMP QueryInterface(REFIID riid, void** ppvObject) override {
        if (!ppvObject) return E_POINTER;
        if (IsEqualGUID(riid, IID_IUnknown) || IsEqualGUID(riid, IID_IClassFactory)) {
            *ppvObject = static_cast<IClassFactory*>(this);
            AddRef();
            return S_OK;
        }
        *ppvObject = NULL;
        return E_NOINTERFACE;
    }

    STDMETHODIMP_(ULONG) AddRef(void) override {
        return (ULONG)InterlockedIncrement(&m_refCount);
    }

    STDMETHODIMP_(ULONG) Release(void) override {
        LONG ref = InterlockedDecrement(&m_refCount);
        if (ref == 0) delete this;
        return (ULONG)ref;
    }

    STDMETHODIMP CreateInstance(IUnknown* pUnkOuter, REFIID riid, void** ppvObject) override {
        if (!ppvObject) return E_POINTER;
        *ppvObject = NULL;
        if (pUnkOuter != NULL && !IsEqualGUID(riid, IID_IUnknown)) {
            return CLASS_E_NOAGGREGATION;
        }

        MusicPadAPO* apo = new (std::nothrow) MusicPadAPO(pUnkOuter);
        if (!apo) return E_OUTOFMEMORY;

        HRESULT hr = apo->NonDelegatingQueryInterface(riid, ppvObject);
        apo->NonDelegatingRelease();
        return hr;
    }

    STDMETHODIMP LockServer(BOOL fLock) override {
        if (fLock) InterlockedIncrement(&g_dllRefCount);
        else       InterlockedDecrement(&g_dllRefCount);
        return S_OK;
    }
};

extern "C" {

HRESULT STDAPICALLTYPE DllGetClassObject(REFCLSID rclsid, REFIID riid, LPVOID* ppv) {
    if (!ppv) return E_POINTER;
    *ppv = NULL;
    if (!IsEqualGUID(rclsid, CLSID_MusicPadAPO)) {
        return CLASS_E_CLASSNOTAVAILABLE;
    }
    MusicPadClassFactory* factory = new (std::nothrow) MusicPadClassFactory();
    if (!factory) return E_OUTOFMEMORY;
    HRESULT hr = factory->QueryInterface(riid, ppv);
    factory->Release();
    return hr;
}

HRESULT STDAPICALLTYPE DllCanUnloadNow(void) {
    return (g_dllRefCount == 0) ? S_OK : S_FALSE;
}

HRESULT STDAPICALLTYPE DllRegisterServer(void) {
    return S_OK;
}

HRESULT STDAPICALLTYPE DllUnregisterServer(void) {
    return S_OK;
}

BOOL WINAPI DllMain(HINSTANCE hinstDLL, DWORD fdwReason, LPVOID lpvReserved) {
    (void)lpvReserved;
    if (fdwReason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(hinstDLL);
    }
    return TRUE;
}

} // extern "C"
