// Compiles MSL with the same options Unity 2022.3's Metal backend uses and reports errors.
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#include <stdlib.h>
#include <string.h>

static id<MTLDevice> gDevice;

static char *dupstr(NSString *s)
{
    const char *u = s ? [s UTF8String] : "";
    char *r = (char *)malloc(strlen(u) + 1);
    strcpy(r, u);
    return r;
}

static id<MTLLibrary> compileLibrary(const char *src, size_t len, int fastMath, NSError **err)
{
    if (!gDevice)
        gDevice = MTLCreateSystemDefaultDevice();
    MTLCompileOptions *opts = [MTLCompileOptions new];
    opts.fastMathEnabled = fastMath ? YES : NO;
    opts.preserveInvariance = YES;
    opts.preprocessorMacros = @{ @"MSL_PRE_HULL_VERTEX_SHADER" : @0, @"MSL_HULL_SHADER" : @0,
                                 @"UNITY_SUPPORT_INDIRECT_BUFFERS" : @1, @"UNITY_SUPPORT_LOD_QUERY" : @1 };
    NSString *s = [[NSString alloc] initWithBytes:src length:len encoding:NSUTF8StringEncoding];
    return [gDevice newLibraryWithSource:s options:opts error:err];
}

// The function constant values the entry point is specialized with
static MTLFunctionConstantValues *constantValues(int kind)
{
    MTLFunctionConstantValues *fc = [MTLFunctionConstantValues new];
    uint32_t remap = 0x76543210;
    BOOL yes = YES;
    if (kind == 0)
        [fc setConstantValue:&yes type:MTLDataTypeBool atIndex:4];
    if (kind == 1)
    {
        [fc setConstantValue:&remap type:MTLDataTypeUInt atIndex:0];
        [fc setConstantValue:&remap type:MTLDataTypeUInt atIndex:1];
        [fc setConstantValue:&yes type:MTLDataTypeBool atIndex:2];
        [fc setConstantValue:&yes type:MTLDataTypeBool atIndex:3];
        [fc setConstantValue:&yes type:MTLDataTypeBool atIndex:5];
    }
    return fc;
}

// kind: 0 = vertex, 1 = fragment, 2 = compute. Returns 0 on success, 1 on library error, 2 on function error.
// *msg receives warnings/errors (malloc'ed, free with mtlcheck_free).
int mtlcheck_compile(const char *src, size_t len, int kind, int fastMath, char **msg)
{
    @autoreleasepool
    {
        NSError *err = nil;
        id<MTLLibrary> lib = compileLibrary(src, len, fastMath, &err);
        if (!lib)
        {
            *msg = dupstr(err ? [err localizedDescription] : @"unknown error");
            return 1;
        }
        NSString *entry = kind == 2 ? @"computeMain" : @"xlatMtlMain";
        NSError *ferr = nil;
        id<MTLFunction> fn = [lib newFunctionWithName:entry constantValues:constantValues(kind) error:&ferr];
        if (!fn)
        {
            *msg = dupstr(ferr ? [ferr localizedDescription] : @"entry point not found");
            return 2;
        }
        // warnings are reported in err even on success
        *msg = dupstr(err ? [err localizedDescription] : @"");
        return 0;
    }
}

// Several sources compiled as one library (put together by mtlcheck.py), then each entry point specialized.
// Returns 1 if the library doesn't compile; else 0, with codes[i] = 0 if entries[i] specialized, 2 if it didn't.
int mtlcheck_compile_batch(const char *src, size_t len, int count, const char *const *entries, const int *kinds,
                           int fastMath, int *codes)
{
    @autoreleasepool
    {
        NSError *err = nil;
        id<MTLLibrary> lib = compileLibrary(src, len, fastMath, &err);
        if (!lib)
            return 1;
        for (int i = 0; i < count; i++)
        {
            NSError *ferr = nil;
            codes[i] = [lib newFunctionWithName:@(entries[i]) constantValues:constantValues(kinds[i]) error:&ferr] ? 0 : 2;
        }
        return 0;
    }
}

void mtlcheck_free(char *p) { free(p); }
