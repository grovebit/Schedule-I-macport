// Thin C ABI around HLSLcc: DXBC -> Metal source + reflection as JSON.
#include "hlslcc.h"
#include "internal_includes/Shader.h"
#include "internal_includes/decode.h"
#include <memory>
#include <string>
#include <vector>
#include <sstream>
#include <cstring>
#include <cstdlib>

namespace {

std::string jstr(const std::string &s)
{
    std::string o = "\"";
    for (unsigned char c : s)
    {
        switch (c)
        {
            case '"': o += "\\\""; break;
            case '\\': o += "\\\\"; break;
            case '\n': o += "\\n"; break;
            case '\r': o += "\\r"; break;
            case '\t': o += "\\t"; break;
            default:
                if (c < 0x20) { char b[8]; snprintf(b, sizeof b, "\\u%04x", c); o += b; }
                else o += (char)c;
        }
    }
    return o + "\"";
}

struct Collector : public HLSLccReflection
{
    std::ostringstream diag, inputs, cbs, consts, cbBinds, texs, bufs, outs, builtins, images;
    bool firstDiag = true, firstIn = true, firstCb = true, firstConst = true, firstCbB = true, firstTex = true, firstBuf = true, firstOut = true, firstBuiltin = true, firstImg = true;
    int curCb = -1;
    unsigned tg[3] = {0, 0, 0};
    long tess[4] = {-1, -1, -1, -1};
    long tessKernel = -1;

    static void sep(std::ostringstream &o, bool &first) { if (!first) o << ","; first = false; }

    void OnDiagnostics(const std::string &error, int line, bool isError) override
    {
        sep(diag, firstDiag); diag << "[" << jstr(error) << "," << line << "," << (isError ? 1 : 0) << "]";
    }
    void OnInputBinding(const std::string &name, int bindIndex) override
    {
        sep(inputs, firstIn); inputs << "[" << jstr(name) << "," << bindIndex << "]";
    }
    bool OnConstantBuffer(const std::string &name, size_t bufferSize, size_t memberCount) override
    {
        ++curCb;
        sep(cbs, firstCb); cbs << "[" << jstr(name) << "," << bufferSize << "," << memberCount << "]";
        return true;
    }
    bool OnConstant(const std::string &name, int bindIndex, SHADER_VARIABLE_TYPE cType, int rows, int cols, bool isMatrix, int arraySize, bool isUsed) override
    {
        sep(consts, firstConst);
        consts << "[" << curCb << "," << jstr(name) << "," << bindIndex << "," << (int)cType << "," << rows << "," << cols << "," << (isMatrix ? 1 : 0) << "," << arraySize << "," << (isUsed ? 1 : 0) << "]";
        return true;
    }
    void OnConstantBufferBinding(const std::string &name, int bindIndex) override
    {
        sep(cbBinds, firstCbB); cbBinds << "[" << jstr(name) << "," << bindIndex << "]";
    }
    void OnTextureBinding(const std::string &name, int bindIndex, int samplerIndex, bool multisampled, HLSLCC_TEX_DIMENSION dim, bool isUAV) override
    {
        sep(texs, firstTex);
        texs << "[" << jstr(name) << "," << bindIndex << "," << samplerIndex << "," << (multisampled ? 1 : 0) << "," << (int)dim << "," << (isUAV ? 1 : 0) << "]";
    }
    void OnBufferBinding(const std::string &name, int bindIndex, bool isUAV) override
    {
        sep(bufs, firstBuf); bufs << "[" << jstr(name) << "," << bindIndex << "," << (isUAV ? 1 : 0) << "]";
    }
    void OnThreadGroupSize(unsigned int x, unsigned int y, unsigned int z) override { tg[0] = x; tg[1] = y; tg[2] = z; }
    void OnTessellationInfo(uint32_t a, uint32_t b, uint32_t c, uint32_t d) override { tess[0] = a; tess[1] = b; tess[2] = c; tess[3] = d; }
    void OnTessellationKernelInfo(uint32_t n) override { tessKernel = n; }
    void OnBuiltinOutput(SPECIAL_NAME name) override { sep(builtins, firstBuiltin); builtins << (int)name; }
    void OnFragmentOutputDeclaration(int numComponents, int outputIndex) override
    {
        sep(outs, firstOut); outs << "[" << numComponents << "," << outputIndex << "]";
    }
    void OnStorageImage(int bindIndex, unsigned int access) override
    {
        sep(images, firstImg); images << "[" << bindIndex << "," << access << "]";
    }
};

void appendSignature(std::ostringstream &o, const std::vector<ShaderInfo::InOutSignature> &sig)
{
    o << "[";
    for (size_t i = 0; i < sig.size(); ++i)
    {
        const ShaderInfo::InOutSignature &s = sig[i];
        if (i) o << ",";
        o << "[" << jstr(s.semanticName) << "," << s.ui32SemanticIndex << "," << (int)s.eSystemValueType << ","
          << (int)s.eComponentType << "," << s.ui32Register << "," << s.ui32Mask << "," << s.ui32ReadWriteMask << "," << (int)s.eMinPrec << "]";
    }
    o << "]";
}

} // namespace

extern "C" {

// Returns malloc'ed JSON string (caller frees with dxbc2metal_free). Never returns NULL.
char *dxbc2metal(const unsigned char *dxbc, size_t len, unsigned int flags)
{
    Collector refl;
    GLSLShader result;
    HLSLccSamplerPrecisionInfo samplerPrecisions;
    GLSLCrossDependencyData deps;
    // copy to aligned buffer (DecodeDXBC reads uint32 tokens)
    std::vector<uint32_t> buf((len + 3) / 4 + 1, 0);
    memcpy(buf.data(), dxbc, len);
    int ok = 0;
    std::string exc;
    try
    {
        ok = TranslateHLSLFromMem((const char *)buf.data(), flags, LANG_METAL, NULL, &deps, samplerPrecisions, refl, &result);
    }
    catch (const std::exception &e) { exc = e.what(); ok = 0; }
    catch (...) { exc = "unknown exception"; ok = 0; }

    std::ostringstream o;
    o << "{\"ok\":" << (ok ? 1 : 0);
    if (!exc.empty()) o << ",\"exception\":" << jstr(exc);
    o << ",\"src\":" << jstr(ok ? result.sourceCode : std::string());
    o << ",\"shaderType\":" << result.shaderType;
    o << ",\"diag\":[" << refl.diag.str() << "]";
    o << ",\"inputs\":[" << refl.inputs.str() << "]";
    o << ",\"cbs\":[" << refl.cbs.str() << "]";
    o << ",\"consts\":[" << refl.consts.str() << "]";
    o << ",\"cbBinds\":[" << refl.cbBinds.str() << "]";
    o << ",\"textures\":[" << refl.texs.str() << "]";
    o << ",\"buffers\":[" << refl.bufs.str() << "]";
    o << ",\"fragOutputs\":[" << refl.outs.str() << "]";
    o << ",\"builtinOutputs\":[" << refl.builtins.str() << "]";
    o << ",\"storageImages\":[" << refl.images.str() << "]";
    o << ",\"threadGroup\":[" << refl.tg[0] << "," << refl.tg[1] << "," << refl.tg[2] << "]";
    o << ",\"tess\":[" << refl.tess[0] << "," << refl.tess[1] << "," << refl.tess[2] << "," << refl.tess[3] << "]";
    o << ",\"tessKernel\":" << refl.tessKernel;
    if (ok)
    {
        o << ",\"inSig\":"; appendSignature(o, result.reflection.psInputSignatures);
        o << ",\"outSig\":"; appendSignature(o, result.reflection.psOutputSignatures);
    }
    o << "}";
    std::string s = o.str();
    char *r = (char *)malloc(s.size() + 1);
    memcpy(r, s.c_str(), s.size() + 1);
    return r;
}

void dxbc2metal_free(char *p) { free(p); }

// Dumps the declarations of a DXBC shader (works without RDEF).
char *dxbc_decls(const unsigned char *dxbc, size_t len)
{
    std::vector<uint32_t> buf((len + 3) / 4 + 1, 0);
    memcpy(buf.data(), dxbc, len);
    std::ostringstream o;
    std::unique_ptr<Shader> sh;
    std::string exc;
    try { sh.reset(DecodeDXBC(buf.data(), 0)); }
    catch (const std::exception &e) { exc = e.what(); }
    catch (...) { exc = "unknown exception"; }
    if (!sh)
    {
        o << "{\"ok\":0,\"exception\":" << jstr(exc) << "}";
    }
    else
    {
        o << "{\"ok\":1,\"shaderType\":" << (int)sh->eShaderType << ",\"major\":" << sh->ui32MajorVersion << ",\"minor\":" << sh->ui32MinorVersion;
        o << ",\"inSig\":"; appendSignature(o, sh->sInfo.psInputSignatures);
        o << ",\"outSig\":"; appendSignature(o, sh->sInfo.psOutputSignatures);
        o << ",\"pcSig\":"; appendSignature(o, sh->sInfo.psPatchConstantSignatures);
        o << ",\"phases\":[";
        for (size_t p = 0; p < sh->asPhases.size(); ++p)
        {
            if (p) o << ",";
            o << "{\"phase\":" << (int)sh->asPhases[p].ePhase << ",\"decls\":[";
            const std::vector<Declaration> &decls = sh->asPhases[p].psDecl;
            for (size_t i = 0; i < decls.size(); ++i)
            {
                const Declaration &d = decls[i];
                if (i) o << ",";
                const Operand &op = d.asOperands[0];
                o << "{\"op\":" << (int)d.eOpcode << ",\"tok\":" << d.ui32OpcodeToken << ",\"rt\":" << d.ui32ReturnTypeToken
                  << ",\"otype\":" << (int)op.eType << ",\"reg\":" << op.ui32RegisterNumber
                  << ",\"idx\":[" << op.aui32ArraySizes[0] << "," << op.aui32ArraySizes[1] << "," << op.aui32ArraySizes[2] << "]"
                  << ",\"nidx\":" << op.iIndexDims
                  << ",\"dim\":" << (int)d.value.eResourceDimension << ",\"stride\":" << d.ui32BufferStride
                  << ",\"tg\":[" << d.value.aui32WorkGroupSize[0] << "," << d.value.aui32WorkGroupSize[1] << "," << d.value.aui32WorkGroupSize[2] << "]"
                  << ",\"counter\":" << (int)d.sUAV.bCounter << "}";
            }
            o << "],\"counterUAVs\":[";
            bool first = true;
            const std::vector<Instruction> &insts = sh->asPhases[p].psInst;
            for (size_t i = 0; i < insts.size(); ++i)
            {
                const Instruction &in = insts[i];
                if (in.eOpcode == OPCODE_IMM_ATOMIC_ALLOC || in.eOpcode == OPCODE_IMM_ATOMIC_CONSUME)
                {
                    if (!first) o << ",";
                    first = false;
                    o << in.asOperands[1].ui32RegisterNumber;
                }
            }
            o << "],\"bufinfo\":[";
            first = true;
            for (size_t i = 0; i < insts.size(); ++i)
            {
                const Instruction &in = insts[i];
                if (in.eOpcode == OPCODE_BUFINFO)
                {
                    if (!first) o << ",";
                    first = false;
                    o << "[" << (in.asOperands[1].eType == OPERAND_TYPE_UNORDERED_ACCESS_VIEW ? 1 : 0) << "," << in.asOperands[1].ui32RegisterNumber << "]";
                }
            }
            o << "]}";
        }
        o << "]}";
    }
    std::string s = o.str();
    char *r = (char *)malloc(s.size() + 1);
    memcpy(r, s.c_str(), s.size() + 1);
    return r;
}

}
