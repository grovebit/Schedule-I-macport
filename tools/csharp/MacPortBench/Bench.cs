// Benchmark harness for the macOS port (only built into "Schedule I Bench.app", see tools/py/bench.py).
// Inactive unless the player is started with -macport-bench. Loads the save given by -macport-bench-save,
// measures frame times for a static view and a 360 degree camera pan, logs "[MacPortBench] RESULT {json}" and quits.
using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Text;
using ScheduleOne.DevUtilities;
using ScheduleOne.Persistence;
using ScheduleOne.PlayerScripts;
using Unity.Profiling;
using UnityEngine;

namespace MacPort
{
    public class Bench : MonoBehaviour
    {
        private const string Tag = "[MacPortBench] ";
        private static Dictionary<string, string> _args;

        public static void Init()
        {
            _args = ParseArgs();
            if (!_args.ContainsKey("-macport-bench"))
                return;
            Application.runInBackground = true;
            var go = new GameObject("MacPortBench");
            DontDestroyOnLoad(go);
            go.AddComponent<Bench>();
        }

        private static Dictionary<string, string> ParseArgs()
        {
            var a = Environment.GetCommandLineArgs();
            var d = new Dictionary<string, string>();
            for (int i = 0; i < a.Length; i++)
            {
                if (!a[i].StartsWith("-macport-"))
                    continue;
                d[a[i]] = i + 1 < a.Length && !a[i + 1].StartsWith("-") ? a[i + 1] : "";
            }
            return d;
        }

        private static string Str(string key, string def) => _args.TryGetValue(key, out var v) && v != "" ? v : def;
        private static float Num(string key, float def) => float.Parse(Str(key, def.ToString(CultureInfo.InvariantCulture)), CultureInfo.InvariantCulture);
        private static void Log(string msg) => Debug.Log(Tag + msg);

        private IEnumerator Start()
        {
            Log($"start device={SystemInfo.graphicsDeviceName} api={SystemInfo.graphicsDeviceType} burst={Unity.Burst.BurstCompiler.IsEnabled} " +
                $"frameTimingStats={FrameTimingManager.IsFeatureEnabled()} cpus={SystemInfo.processorCount}");
            Log("resolutions: " + string.Join(", ", DisplaySettings.GetResolutions().Select((r, i) => $"{i}:{r.width}x{r.height}")));
            yield return WaitFor(() => Singleton<LoadManager>.InstanceExists, 120f);
            yield return new WaitForSecondsRealtime(Num("-macport-bench-menu-wait", 5f));
            ApplyBenchDisplay();
            yield return new WaitForSecondsRealtime(1f);
            float menu = Num("-macport-bench-menu", 10f);
            if (menu > 0f)
                yield return Measure("menu", menu, null);

            string save = Str("-macport-bench-save", "");
            if (save == "")
            {
                Log("no -macport-bench-save given, done");
                Finish();
                yield break;
            }
            if (!LoadManager.TryLoadSaveInfo(save, 0, out var info))
            {
                Log("ERROR: cannot read save " + save);
                Finish();
                yield break;
            }
            var lm = Singleton<LoadManager>.Instance;
            float t0 = Time.realtimeSinceStartup;
            lm.StartGame(info, allowLoadStacking: false, allowSaveBackup: false);
            yield return WaitFor(() => lm.IsGameLoaded && !lm.IsLoading && PlayerSingleton<PlayerCamera>.InstanceExists, 600f);
            Log($"loaded in {Time.realtimeSinceStartup - t0:F1}s");
            yield return new WaitForSecondsRealtime(Num("-macport-bench-warmup", 15f));
            string teleport = Str("-macport-bench-teleport", "");
            if (teleport != "")
            {
                // same as the game's "teleport <place>" console command (townhall, docks, ...)
                var points = Singleton<ScheduleOne.Console>.Instance.TeleportPointsContainer;
                Log("teleport points: " + string.Join(", ", Enumerable.Range(0, points.childCount).Select(i => points.GetChild(i).name)));
                new ScheduleOne.Console.Teleport().Execute(new List<string> { teleport });
                Log("teleported to " + teleport + " " + PlayerSingleton<PlayerCamera>.Instance.transform.position);
                yield return new WaitForSecondsRealtime(Num("-macport-bench-teleport-wait", 10f));
            }
            LogNavigation();
            ApplyBenchDisplay();
            yield return new WaitForSecondsRealtime(2f);
            string shot = Str("-macport-bench-shot", "");
            if (shot != "")
            {
                ScreenCapture.CaptureScreenshot(shot);  // the game's own frame only
                yield return new WaitForSecondsRealtime(1f);
                Log("screenshot " + shot);
            }
            yield return Measure("static", Num("-macport-bench-static", 20f), null);

            var cam = PlayerSingleton<PlayerCamera>.Instance;
            var t = cam.transform;
            Vector3 pos = t.position;
            Quaternion rot0 = t.rotation;
            cam.OverrideTransform(pos, rot0, 0f, keepParented: true);
            yield return null;
            yield return Measure("pan", Num("-macport-bench-pan", 30f), p => t.rotation = Quaternion.AngleAxis(360f * p, Vector3.up) * rot0);
            cam.StopTransformOverride(0f);
            float profile = Num("-macport-bench-profile", 0f);
            if (profile > 0f)
                yield return Profile(profile);
            Finish();
        }

        private static List<(string name, string cat, ProfilerRecorder rec)> _recs;
        private static double[] _sums, _counts;
        private static int _frames;

        // Runs first in every player loop iteration, i.e. after the previous frame completed: collect and clear.
        private static void CollectFrame()
        {
            _frames++;
            for (int i = 0; i < _recs.Count; i++)
            {
                var rec = _recs[i].rec;
                for (int k = 0, n = rec.Count; k < n; k++)
                {
                    var s = rec.GetSample(k);
                    _sums[i] += s.Value;
                    _counts[i] += s.Count;
                }
                rec.Reset();
                if (!rec.IsRunning)
                    rec.Start();
            }
        }

        // Development players only: records every time marker the engine knows and logs the most expensive ones.
        private IEnumerator Profile(float seconds)
        {
            UnityEngine.Profiling.Profiler.enabled = true;  // also emits per-script markers ("X.Update() [Invoke]")
            yield return new WaitForSecondsRealtime(2f);
            var handles = new List<Unity.Profiling.LowLevel.Unsafe.ProfilerRecorderHandle>();
            Unity.Profiling.LowLevel.Unsafe.ProfilerRecorderHandle.GetAvailable(handles);
            var recs = new List<(string name, string cat, ProfilerRecorder rec)>();
            foreach (var hnd in handles)
            {
                var desc = Unity.Profiling.LowLevel.Unsafe.ProfilerRecorderHandle.GetDescription(hnd);
                if (desc.UnitType != ProfilerMarkerDataUnit.TimeNanoseconds)
                    continue;
                var rec = new ProfilerRecorder(hnd, 16, ProfilerRecorderOptions.SumAllSamplesInFrame | ProfilerRecorderOptions.WrapAroundWhenCapacityReached);
                rec.Start();
                recs.Add((desc.Name, desc.Category.Name, rec));
            }
            Log($"profile: recording {recs.Count} of {handles.Count} markers for {seconds}s");
            _recs = recs;
            _frames = 0;
            var sums = _sums = new double[recs.Count];
            var counts = _counts = new double[recs.Count];
            var original = UnityEngine.LowLevel.PlayerLoop.GetCurrentPlayerLoop();
            var loop = original;
            // the previous frame's samples are committed during Initialization: collect at the start of EarlyUpdate
            var phases = (UnityEngine.LowLevel.PlayerLoopSystem[])original.subSystemList.Clone();
            int early = Array.FindIndex(phases, ph => ph.type == typeof(UnityEngine.PlayerLoop.EarlyUpdate));
            var systems = new List<UnityEngine.LowLevel.PlayerLoopSystem>(phases[early].subSystemList);
            systems.Insert(0, new UnityEngine.LowLevel.PlayerLoopSystem { type = typeof(Bench), updateDelegate = CollectFrame });
            phases[early].subSystemList = systems.ToArray();
            loop.subSystemList = phases;
            UnityEngine.LowLevel.PlayerLoop.SetPlayerLoop(loop);
            yield return new WaitForSecondsRealtime(seconds);
            UnityEngine.LowLevel.PlayerLoop.SetPlayerLoop(original);
            UnityEngine.Profiling.Profiler.enabled = false;
            int frames = Math.Max(_frames - 1, 1);  // the first collection holds the partial frame before the hook
            // some markers report timestamps or nested job bookkeeping rather than durations: skip absurd values
            var valid = Enumerable.Range(0, recs.Count).Where(i => sums[i] / frames * 1e-6 < 100.0).ToList();
            Log($"profile: {frames} frames collected");
            void Report(string title, IEnumerable<int> items)
            {
                Log("PROFILE --- " + title);
                foreach (int i in items)
                    Log(string.Format(CultureInfo.InvariantCulture, "PROFILE {0,8:0.000} ms/frame {1,7:0.0} calls/frame  [{2}] {3}",
                        sums[i] / frames * 1e-6, counts[i] / frames, recs[i].cat, recs[i].name));
            }
            int top = (int)Num("-macport-bench-profile-top", 60f);
            Report("all threads", valid.OrderByDescending(i => sums[i]).Take(top));
            foreach (var cat in new[] { "PlayerLoop", "Scripts", "GC", "Physics", "Animation", "AI", "Network", "UI Render", "Audio" })
                Report(cat, valid.Where(i => recs[i].cat == cat).OrderByDescending(i => sums[i]).Take(top));
            Report("game code ([Invoke] markers)", valid.Where(i => recs[i].name.Contains("[Invoke]") || recs[i].name.Contains("[Coroutine")).OrderByDescending(i => sums[i]).Take(top));
            foreach (var r in recs)
                r.rec.Dispose();
        }

        // Sanity check that NPC navigation works (NavMesh loaded, agents placed on it and moving).
        private static void LogNavigation()
        {
            var agents = FindObjectsOfType<UnityEngine.AI.NavMeshAgent>();
            int enabled = agents.Count(a => a.isActiveAndEnabled);
            int onMesh = agents.Count(a => a.isActiveAndEnabled && a.isOnNavMesh);
            int moving = agents.Count(a => a.isActiveAndEnabled && a.velocity.sqrMagnitude > 0.01f);
            int verts = UnityEngine.AI.NavMesh.CalculateTriangulation().vertices.Length;
            Log($"navigation: navmesh vertices={verts} agents={agents.Length} enabled={enabled} onNavMesh={onMesh} moving={moving}");
        }

        private void Finish()
        {
            Log("done");
            if (Str("-macport-bench-quit", "1") != "0")
                Application.Quit();
        }

        private static void ApplyBenchDisplay()
        {
            // uncapped, no vsync: measures how fast frames can be produced
            QualitySettings.vSyncCount = 0;
            Application.targetFrameRate = (int)Num("-macport-bench-fps", -1f);
            string res = Str("-macport-bench-res", "");
            if (res != "")
            {
                var wh = res.Split('x');
                Screen.SetResolution(int.Parse(wh[0]), int.Parse(wh[1]), FullScreenMode.Windowed);
            }
        }

        private static IEnumerator WaitFor(Func<bool> cond, float timeout)
        {
            float end = Time.realtimeSinceStartup + timeout;
            while (!cond())
            {
                if (Time.realtimeSinceStartup > end)
                {
                    Log("ERROR: timeout waiting");
                    yield break;
                }
                yield return null;
            }
        }

        private IEnumerator Measure(string phase, float seconds, Action<float> step)
        {
            var frames = new List<float>(4096);
            double cpuMain = 0, cpuRender = 0, gpu = 0;
            int timed = 0;
            var timings = new FrameTiming[1];
            using (var mainRec = ProfilerRecorder.StartNew(ProfilerCategory.Internal, "Main Thread", 1))
            using (var gcRec = ProfilerRecorder.StartNew(ProfilerCategory.Memory, "GC Allocated In Frame", 1))
            using (var batchRec = ProfilerRecorder.StartNew(ProfilerCategory.Render, "Batches Count", 1))
            using (var setPassRec = ProfilerRecorder.StartNew(ProfilerCategory.Render, "SetPass Calls Count", 1))
            {
                double mainSum = 0, gcSum = 0, batchSum = 0, setPassSum = 0;
                yield return null;
                float start = Time.realtimeSinceStartup;
                while (true)
                {
                    float p = (Time.realtimeSinceStartup - start) / seconds;
                    if (p >= 1f)
                        break;
                    step?.Invoke(p);
                    yield return null;
                    frames.Add(Time.unscaledDeltaTime * 1000f);
                    mainSum += mainRec.Valid ? mainRec.LastValue * 1e-6 : 0;
                    gcSum += gcRec.Valid ? gcRec.LastValue : 0;
                    batchSum += batchRec.Valid ? batchRec.LastValue : 0;
                    setPassSum += setPassRec.Valid ? setPassRec.LastValue : 0;
                    FrameTimingManager.CaptureFrameTimings();
                    if (FrameTimingManager.GetLatestTimings(1, timings) == 1 && timings[0].gpuFrameTime > 0)
                    {
                        cpuMain += timings[0].cpuMainThreadFrameTime;
                        cpuRender += timings[0].cpuRenderThreadFrameTime;
                        gpu += timings[0].gpuFrameTime;
                        timed++;
                    }
                }
                float elapsed = Time.realtimeSinceStartup - start;
                int n = Math.Max(frames.Count, 1);
                var sorted = frames.OrderBy(x => x).ToArray();
                float Pct(float q) => sorted.Length == 0 ? 0 : sorted[Math.Min(sorted.Length - 1, (int)(q * sorted.Length))];
                var sb = new StringBuilder("{");
                void Add(string k, double v) => sb.Append(sb.Length > 1 ? "," : "").Append('"').Append(k).Append("\":").Append(v.ToString("0.###", CultureInfo.InvariantCulture));
                sb.Append("\"phase\":\"").Append(phase).Append('"');
                Add("frames", frames.Count);
                Add("fps", frames.Count / elapsed);
                Add("ms_avg", frames.Count == 0 ? 0 : frames.Average());
                Add("ms_p50", Pct(0.5f));
                Add("ms_p95", Pct(0.95f));
                Add("ms_p99", Pct(0.99f));
                Add("ms_max", sorted.Length == 0 ? 0 : sorted[sorted.Length - 1]);
                Add("hitches_50", frames.Count(x => x > 50f));
                Add("hitches_100", frames.Count(x => x > 100f));
                Add("main_thread_ms", mainRec.Valid ? mainSum / n : -1);
                Add("cpu_main_ms", timed > 0 ? cpuMain / timed : -1);
                Add("cpu_render_ms", timed > 0 ? cpuRender / timed : -1);
                Add("gpu_ms", timed > 0 ? gpu / timed : -1);
                Add("gc_kb_per_frame", gcRec.Valid ? gcSum / n / 1024.0 : -1);
                Add("batches", batchRec.Valid ? batchSum / n : -1);
                Add("setpass", setPassRec.Valid ? setPassSum / n : -1);
                Add("w", Screen.width);
                Add("h", Screen.height);
                Add("quality", QualitySettings.GetQualityLevel());
                sb.Append('}');
                Log("RESULT " + sb);
            }
        }
    }
}
