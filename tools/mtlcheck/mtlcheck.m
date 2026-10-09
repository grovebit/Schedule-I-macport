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

// kind: 0 = vertex, 1 = fragment, 2 = compute. Returns 0 on success, 1 on library error, 2 on function error.
// *msg receives warnings/errors (malloc'ed, free with mtlcheck_free).
int mtlcheck_compile(const char *src, size_t len, int kind, int fastMath, char **msg)
{
    @autoreleasepool
    {
        if (!gDevice)
            gDevice = MTLCreateSystemDefaultDevice();
        MTLCompileOptions *opts = [MTLCompileOptions new];
        opts.fastMathEnabled = fastMath ? YES : NO;
        opts.preserveInvariance = YES;
        opts.preprocessorMacros = @{ @"MSL_PRE_HULL_VERTEX_SHADER" : @0, @"MSL_HULL_SHADER" : @0,
                                     @"UNITY_SUPPORT_INDIRECT_BUFFERS" : @1, @"UNITY_SUPPORT_LOD_QUERY" : @1 };
        NSString *s = [[NSString alloc] initWithBytes:src length:len encoding:NSUTF8StringEncoding];
        NSError *err = nil;
        id<MTLLibrary> lib = [gDevice newLibraryWithSource:s options:opts error:&err];
        if (!lib)
        {
            *msg = dupstr(err ? [err localizedDescription] : @"unknown error");
            return 1;
        }
        NSString *entry = kind == 2 ? @"computeMain" : @"xlatMtlMain";
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
        NSError *ferr = nil;
        id<MTLFunction> fn = [lib newFunctionWithName:entry constantValues:fc error:&ferr];
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

void mtlcheck_free(char *p) { free(p); }
