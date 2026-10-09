// Dumps the externally visible API (public/protected/internal members) of an assembly, one sorted line per member.
using Mono.Cecil;
var asm = AssemblyDefinition.ReadAssembly(args[0]);
var lines = new List<string>();
foreach (var t in asm.MainModule.GetTypes())
{
    if (t.Name.StartsWith("<") || t.FullName.Contains("UnitySourceGenerated")) continue;
    lines.Add($"T {t.FullName} : {t.BaseType?.FullName}");
    foreach (var m in t.Methods.Where(m => !m.IsPrivate))
        lines.Add($"M {t.FullName}::{m.Name}({string.Join(",", m.Parameters.Select(p => p.ParameterType.FullName))}) -> {m.ReturnType.FullName}");
    foreach (var f in t.Fields.Where(f => !f.IsPrivate))
        lines.Add($"F {t.FullName}::{f.Name} : {f.FieldType.FullName}");
    foreach (var p in t.Properties)
        lines.Add($"P {t.FullName}::{p.Name} : {p.PropertyType.FullName}");
}
lines.Sort(StringComparer.Ordinal);
foreach (var l in lines) Console.WriteLine(l);
