// Runtime options for the macOS port, called at startup through Unity's RuntimeInitializeOnLoads.json
// (registered by tools/py/assemble_app.py).
//
// Render scale: PlayerPrefs "MacPortRenderScale" (e.g. `defaults write com.TVGS.ScheduleI MacPortRenderScale -float 0.75`)
// or the -macport-render-scale <x> launch argument. Below 1 the 3D scene is rendered at that fraction of the
// output resolution and upscaled with URP's FSR 1.0; the UI stays at full resolution.
using System;
using System.Globalization;
using UnityEngine;
using UnityEngine.Rendering;
using UnityEngine.Rendering.Universal;

namespace MacPort
{
    public static class Runtime
    {
        public const string RenderScaleKey = "MacPortRenderScale";

        public static void Init()
        {
            float scale = RenderScale();
            if (scale >= 0.999f)
                return;
            int applied = 0;
            for (int i = 0; i < QualitySettings.names.Length; i++)
                applied += Apply(QualitySettings.GetRenderPipelineAssetAt(i), scale);
            applied += Apply(GraphicsSettings.defaultRenderPipeline, scale);
            Debug.Log($"[MacPort] render scale {scale:0.##} with FSR 1.0 upscaling ({applied} pipeline assets)");
        }

        private static float RenderScale()
        {
            float scale = PlayerPrefs.GetFloat(RenderScaleKey, 1f);
            var args = Environment.GetCommandLineArgs();
            for (int i = 0; i + 1 < args.Length; i++)
            {
                if (args[i] == "-macport-render-scale")
                    float.TryParse(args[i + 1], NumberStyles.Float, CultureInfo.InvariantCulture, out scale);
            }
            return Mathf.Clamp(scale, 0.5f, 1f);
        }

        private static int Apply(RenderPipelineAsset asset, float scale)
        {
            if (!(asset is UniversalRenderPipelineAsset urp))
                return 0;
            urp.renderScale = scale;
            urp.upscalingFilter = UpscalingFilterSelection.FSR;
            return 1;
        }
    }
}
