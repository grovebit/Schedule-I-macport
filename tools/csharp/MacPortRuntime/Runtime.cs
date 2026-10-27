// Runtime options for the macOS port, called at startup through Unity's RuntimeInitializeOnLoads.json
// (registered by tools/py/assemble_app.py).
//
// Render scale: PlayerPrefs "MacPortRenderScale" (e.g. `defaults write com.TVGS.ScheduleI MacPortRenderScale -float 0.75`)
// or the -macport-render-scale <x> launch argument. Below 1 the 3D scene is rendered at that fraction of the
// output resolution and upscaled with URP's FSR 1.0; the UI stays at full resolution.
//
// Renderer feature settings: PlayerPrefs "MacPortFeatures" or -macport-features <list>, a comma separated list of
// <feature name>.<field>[.<field>...]=<value> (enums by name, bools as 0/1), set by reflection on every URP renderer,
// e.g. "GodRaysRenderFeature.settings.temporallyRender=1,ScreenSpaceAmbientOcclusion.m_Settings.Downsample=1".
// Without either, DefaultFeatures applies (set "MacPortFeatures" to "" for the game's own settings).
using System;
using System.Collections;
using System.Globalization;
using System.Reflection;
using UnityEngine;
using UnityEngine.Rendering;
using UnityEngine.Rendering.Universal;

namespace MacPort
{
    public static class Runtime
    {
        public const string RenderScaleKey = "MacPortRenderScale";
        public const string FeaturesKey = "MacPortFeatures";

        // Measured with tools/py/bench.py on an M2 Pro (3200x2000, 75% render scale): god rays from 2.4 to 0.6 ms
        // and SSAO from 1.9 to 0.9 ms per frame; screenshots differ from the game's settings no more than two runs
        // of the game's settings do.
        public const string DefaultFeatures =
            "GodRaysRenderFeature.settings.textureQuality=Low,ScreenSpaceAmbientOcclusion.m_Settings.Downsample=1";

        public static void Init()
        {
            ApplyFeatureSettings(Option("-macport-features", PlayerPrefs.GetString(FeaturesKey, DefaultFeatures)));
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

        private static string Option(string name, string def)
        {
            var args = Environment.GetCommandLineArgs();
            for (int i = 0; i + 1 < args.Length; i++)
            {
                if (args[i] == name)
                    return args[i + 1];
            }
            return def;
        }

        private const BindingFlags Members = BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic;

        private static FieldInfo FindField(Type type, string name)
        {
            for (; type != null; type = type.BaseType)
            {
                var field = type.GetField(name, Members | BindingFlags.DeclaredOnly);
                if (field != null)
                    return field;
            }
            return null;
        }

        private static void ApplyFeatureSettings(string list)
        {
            if (string.IsNullOrWhiteSpace(list))
                return;
            var assets = new System.Collections.Generic.List<UniversalRenderPipelineAsset>();
            for (int i = 0; i <= QualitySettings.names.Length; i++)
            {
                var asset = i < QualitySettings.names.Length ? QualitySettings.GetRenderPipelineAssetAt(i) : GraphicsSettings.defaultRenderPipeline;
                if (asset is UniversalRenderPipelineAsset a && !assets.Contains(a))
                    assets.Add(a);
            }
            foreach (var entry in list.Split(','))
            {
                int eq = entry.IndexOf('=');
                if (eq < 0)
                    continue;
                var path = entry.Substring(0, eq).Trim().Split('.');
                string value = entry.Substring(eq + 1).Trim();
                int applied = 0;
                foreach (var asset in assets)
                {
                    if (!(FindField(asset.GetType(), "m_RendererDataList")?.GetValue(asset) is IEnumerable renderers))
                        continue;
                    foreach (ScriptableRendererData data in renderers)
                    {
                        if (data == null)
                            continue;
                        foreach (var feature in data.rendererFeatures)
                        {
                            if (feature != null && feature.name == path[0] && SetPath(feature, path, 1, value))
                                applied++;
                        }
                    }
                }
                Debug.Log($"[MacPort] {entry.Trim()}: " + (applied > 0 ? $"set on {applied} renderer feature(s)" : "no matching feature or field"));
            }
        }

        // Sets obj.path[i].path[i+1]...=value; boxed structs on the way are written back.
        private static bool SetPath(object obj, string[] path, int i, string value)
        {
            var field = FindField(obj.GetType(), path[i]);
            if (field == null)
                return false;
            if (i == path.Length - 1)
            {
                var t = field.FieldType;
                object v = t.IsEnum ? Enum.Parse(t, value, true)
                    : t == typeof(bool) ? (object)(value == "1" || value.Equals("true", StringComparison.OrdinalIgnoreCase))
                    : Convert.ChangeType(value, t, CultureInfo.InvariantCulture);
                field.SetValue(obj, v);
                return true;
            }
            var child = field.GetValue(obj);
            if (child == null || !SetPath(child, path, i + 1, value))
                return false;
            if (field.FieldType.IsValueType)
                field.SetValue(obj, child);
            return true;
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
