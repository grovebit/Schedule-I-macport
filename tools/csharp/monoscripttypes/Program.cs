// Writes the UnitySourceGeneratedAssemblyMonoScriptTypes_v1 class of one of the game's assemblies as C# source.
// Unity's build adds this class to every assembly: the assembly's script file paths and the types they declare,
// which the player uses to match the scripts referenced by scenes and prefabs to classes. The macOS rebuilds of
// Steamworks.NET and the Input System (work/build) must carry the same table as the game's copies.
//   monoscripttypes <assembly.dll> <out.cs>    (out.cs is only rewritten when its content changes)
using System.Text;
using Mono.Cecil;
using Mono.Cecil.Cil;

const string ClassName = "UnitySourceGeneratedAssemblyMonoScriptTypes_v1";
var (input, output) = (args[0], args[1]);
var module = ModuleDefinition.ReadModule(input);
var get = module.GetType(ClassName)?.Methods.FirstOrDefault(m => m.Name == "Get")
    ?? throw new InvalidOperationException($"{input} has no {ClassName}.Get()");

// Get() fills a MonoScriptData struct: the byte arrays with newarr + RuntimeHelpers.InitializeArray from a field's
// initial data (ldtoken), the counts and the flag with constants. Collect the stored values in IL order.
var fields = new List<(string Name, string Type, object Value)>();
object value = null;
foreach (var ins in get.Body.Instructions)
{
    if (ins.OpCode.Code == Code.Ldtoken && ins.Operand is FieldReference data)
        value = data.Resolve().InitialValue;
    else if (Constant(ins) is int c)
        value = c;
    else if (ins.OpCode.Code == Code.Stfld && ins.Operand is FieldReference field)
    {
        var type = field.FieldType.FullName;
        if (type == "System.Byte[]" && value is int length)
            value = new byte[length];  // an empty array has no initial data
        fields.Add((field.Name, type, value));
    }
}

// The same layout as the class Unity's build generates
var sb = new StringBuilder();
void Line(int indent, string text) => sb.Append('\t', indent).Append(text).Append('\n');
foreach (var line in new[] { "using System.CodeDom.Compiler;", "using System.ComponentModel;",
             "using System.Runtime.CompilerServices;", "", "[CompilerGenerated]",
             "[EditorBrowsable(EditorBrowsableState.Never)]",
             "[GeneratedCode(\"Unity.MonoScriptGenerator.MonoScriptInfoGenerator\", null)]", "internal class " + ClassName, "{" })
    Line(0, line);
Line(1, "private struct MonoScriptData");
Line(1, "{");
Line(2, "public byte[] FilePathsData;");
Line(0, "");
Line(2, "public byte[] TypesData;");
Line(0, "");
Line(2, "public int TotalTypes;");
Line(0, "");
Line(2, "public int TotalFiles;");
Line(0, "");
Line(2, "public bool IsEditorOnly;");
Line(1, "}");
Line(0, "");
Line(1, "[MethodImpl(MethodImplOptions.AggressiveInlining)]");
Line(1, "private static MonoScriptData Get()");
Line(1, "{");
Line(2, "return new MonoScriptData");
Line(2, "{");
for (var i = 0; i < fields.Count; i++)
{
    var (name, type, v) = fields[i];
    var comma = i < fields.Count - 1 ? "," : "";
    if (v is byte[] bytes)
    {
        Line(3, $"{name} = new byte[{bytes.Length}]" + (bytes.Length == 0 ? comma : ""));
        if (bytes.Length == 0)
            continue;
        Line(3, "{");
        for (var j = 0; j < bytes.Length; j += 10)
            Line(4, string.Join(", ", bytes.Skip(j).Take(10)) + (j + 10 < bytes.Length ? "," : ""));
        Line(3, "}" + comma);
    }
    else
        Line(3, $"{name} = " + (type == "System.Boolean" ? ((int)v != 0 ? "true" : "false") : v.ToString()) + comma);
}
Line(2, "};");
Line(1, "}");
Line(0, "}");

var text = sb.ToString();
if (!File.Exists(output) || File.ReadAllText(output) != text)
    File.WriteAllText(output, text);
var counts = fields.Where(f => f.Name is "TotalFiles" or "TotalTypes").Select(f => $"{f.Value} {f.Name[5..].ToLowerInvariant()}");
Console.WriteLine($"{Path.GetFileName(input)}: {string.Join(", ", counts)}");

static int? Constant(Instruction i) => i.OpCode.Code switch
{
    Code.Ldc_I4_M1 => -1,
    Code.Ldc_I4_0 => 0, Code.Ldc_I4_1 => 1, Code.Ldc_I4_2 => 2, Code.Ldc_I4_3 => 3, Code.Ldc_I4_4 => 4,
    Code.Ldc_I4_5 => 5, Code.Ldc_I4_6 => 6, Code.Ldc_I4_7 => 7, Code.Ldc_I4_8 => 8,
    Code.Ldc_I4_S => (sbyte)i.Operand,
    Code.Ldc_I4 => (int)i.Operand,
    _ => null,
};
