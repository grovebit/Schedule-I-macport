// Runtime shims used by the macOS port (referenced from IL-patched game assemblies).
using UnityEngine;

namespace MacPort
{
    public static class ComputeCompat
    {
        public const string BufferSizePrefix = "_mp_BufSize_";

        // Metal shaders cannot query buffer sizes (HLSL GetDimensions); the converted kernels read the
        // element count from a hidden constant named _mp_BufSize_<buffer> instead.
        public static void SetBuffer(ComputeShader shader, int kernelIndex, string name, ComputeBuffer buffer)
        {
            shader.SetBuffer(kernelIndex, name, buffer);
            if (buffer != null)
                shader.SetInt(BufferSizePrefix + name, buffer.count);
        }

        public static void SetBuffer(ComputeShader shader, int kernelIndex, string name, GraphicsBuffer buffer)
        {
            shader.SetBuffer(kernelIndex, name, buffer);
            if (buffer != null)
                shader.SetInt(BufferSizePrefix + name, buffer.count);
        }
    }
}
