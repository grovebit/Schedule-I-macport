// IL patches applied to the game's managed assemblies for the macOS port.
//   ilpatch <in.dll> <out.dll> <ManagedDir> <MacPortCompat.dll>
using Mono.Cecil;
using Mono.Cecil.Cil;

var (input, output, managedDir, helperPath) = (args[0], args[1], args[2], args[3]);
var resolver = new DefaultAssemblyResolver();
resolver.AddSearchDirectory(managedDir);
resolver.AddSearchDirectory(Path.GetDirectoryName(Path.GetFullPath(helperPath)));
var asm = AssemblyDefinition.ReadAssembly(input, new ReaderParameters { AssemblyResolver = resolver, InMemory = true });
var helper = AssemblyDefinition.ReadAssembly(helperPath, new ReaderParameters { AssemblyResolver = resolver });
var compat = helper.MainModule.GetType("MacPort.ComputeCompat");
MethodReference Helper(string bufferType) =>
    asm.MainModule.ImportReference(compat.Methods.First(m => m.Name == "SetBuffer" && m.Parameters[3].ParameterType.Name == bufferType));

int patched = 0;
foreach (var type in asm.MainModule.GetTypes())
foreach (var method in type.Methods.Where(m => m.HasBody))
foreach (var ins in method.Body.Instructions)
{
    if ((ins.OpCode != OpCodes.Callvirt && ins.OpCode != OpCodes.Call) || ins.Operand is not MethodReference mr)
        continue;
    if (mr.DeclaringType.FullName != "UnityEngine.ComputeShader" || mr.Name != "SetBuffer" || mr.Parameters.Count != 3)
        continue;
    if (mr.Parameters[0].ParameterType.FullName != "System.Int32" || mr.Parameters[1].ParameterType.FullName != "System.String")
        continue;
    var bufType = mr.Parameters[2].ParameterType.Name;
    if (bufType != "ComputeBuffer" && bufType != "GraphicsBuffer")
        continue;
    ins.OpCode = OpCodes.Call;
    ins.Operand = Helper(bufType);
    patched++;
    Console.WriteLine($"  {type.FullName}::{method.Name} -> MacPort.ComputeCompat.SetBuffer({bufType})");
}
asm.Write(output);
Console.WriteLine($"patched {patched} ComputeShader.SetBuffer call(s) in {Path.GetFileName(input)}");
