// The app's executable, in place of Unity's stub that only calls PlayerMain (Source/Player/MacPlayer/
// MacPlayerEntryPoint/Main.cpp in Unity's macOS player module). Before starting the game it checks whether the game
// folder the app was built from changed since, i.e. Steam updated the game, and if so rebuilds the app with
// ./build.sh while showing a progress panel. "launcher --check" just reports whether the game folder changed.
//
// Unity's player creates the NSApplication in this process, so this mustn't use AppKit: the panels are drawn by the
// system (CFUserNotification), and after a rebuild the new executable takes over this process (exec).
#import <Foundation/Foundation.h>
#include <fcntl.h>
#include <mach-o/dyld.h>
#include <signal.h>
#include <spawn.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

extern char **environ;

// Unity's entry point (see UnityPlayer.tbd), weakly linked so that the launcher also runs without Unity's library
int PlayerMain(int argc, const char *argv[]) __asm__("__Z10PlayerMainiPPKc") __attribute__((weak_import));

static const char *const kSkipCheck = "MACPORT_SKIP_UPDATE_CHECK";  // set for the app taking over after a build
static NSURL *gIcon;
static volatile sig_atomic_t gQuit;  // asked to quit (SIGTERM etc.) during an update

// The first file of the game folder that differs from when the app was built, or nil if none does or there is
// nothing to compare with. Resources/MacPortGameFiles.txt (from build.py) lists the files as "size mtime_ns path".
// Only the files' metadata is read: reading the files themselves on an external drive (or in Documents etc.) would
// make macOS ask for permission after each rebuild, as the rebuilt app, ad-hoc signed, is new to it.
static NSString *changedGameFile(NSBundle *bundle, NSString *game)
{
    if (![game isKindOfClass:NSString.class])
        return nil;
    NSString *data = [game stringByAppendingPathComponent:@"Schedule I_Data"];
    struct stat st;
    if (stat(data.fileSystemRepresentation, &st) != 0)
        return nil;  // gone (e.g. uninstalled): the app has everything it needs
    NSString *list = [NSString stringWithContentsOfFile:[bundle pathForResource:@"MacPortGameFiles" ofType:@"txt"]
                                               encoding:NSUTF8StringEncoding error:nil];
    __block NSString *changed = nil;
    [list enumerateLinesUsingBlock:^(NSString *line, BOOL *stop) {
        long long size, mtime;
        int path = 0;
        if (sscanf(line.UTF8String, "%lld %lld %n", &size, &mtime, &path) != 2 || !path)
            return;
        NSString *file = [line substringFromIndex:path];  // after the digits and spaces, so the offset is the same
        struct stat f;
        if (stat([data stringByAppendingPathComponent:file].fileSystemRepresentation, &f) != 0 || f.st_size != size ||
            f.st_mtimespec.tv_sec * 1000000000LL + f.st_mtimespec.tv_nsec != mtime)
        {
            changed = file;
            *stop = YES;
        }
    }];
    return changed;
}

// ./build.sh of the repository the app is in (in its build folder), else of the one that built it; nil if neither
// is there anymore
static NSString *buildScript(NSString *app, NSString *repository)
{
    NSMutableArray<NSString *> *repositories = [NSMutableArray array];
    NSString *folder = app.stringByDeletingLastPathComponent;
    if ([folder.lastPathComponent isEqualToString:@"build"])
        [repositories addObject:folder.stringByDeletingLastPathComponent];
    if ([repository isKindOfClass:NSString.class])
        [repositories addObject:repository];
    for (NSString *r in repositories)
    {
        NSString *script = [r stringByAppendingPathComponent:@"build.sh"];
        if (access(script.fileSystemRepresentation, X_OK) == 0 &&
            access([r stringByAppendingPathComponent:@"tools/py/build.py"].fileSystemRepresentation, R_OK) == 0)
            return script;
    }
    return nil;
}

static NSString *readText(NSString *path)
{
    NSData *data = [NSData dataWithContentsOfFile:path];
    if (!data)
        return @"";
    return [[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding]
               ?: [[NSString alloc] initWithData:data encoding:NSISOLatin1StringEncoding];
}

// The step build.py is at (its "==> step" lines), for the progress panel
static NSString *buildStep(NSString *log)
{
    NSDictionary *steps = @{@"game" : @"Checking the game files", @"deps" : @"Downloading the tools",
                            @"tools" : @"Building the tools", @"shaders" : @"Converting the shaders",
                            @"assemblies" : @"Building the libraries", @"icon" : @"Extracting the icon",
                            @"app" : @"Assembling the app"};
    __block NSString *step = nil;
    [readText(log) enumerateLinesUsingBlock:^(NSString *line, BOOL *stop) {
        if ([line hasPrefix:@"==> "])
            step = [line substringFromIndex:4];
    }];
    return step ? (steps[step] ?: step) : @"Starting";
}

// What a failed build said: from its last "error: " line on (the messages of build.py and build.sh), else its last
// lines
static NSString *buildError(NSString *log)
{
    NSString *text = [readText(log) stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceAndNewlineCharacterSet];
    NSArray<NSString *> *lines = [text componentsSeparatedByString:@"\n"];
    NSUInteger from = lines.count > 8 ? lines.count - 8 : 0;
    for (NSUInteger i = 0; i < lines.count; i++)
        if ([lines[i] hasPrefix:@"error: "])
            from = i;
    NSString *error = [[lines subarrayWithRange:NSMakeRange(from, lines.count - from)] componentsJoinedByString:@"\n"];
    if ([error hasPrefix:@"error: "])
        error = [error substringFromIndex:7];
    if (error.length > 1500)
        error = [@"…" stringByAppendingString:[error substringFromIndex:error.length - 1500]];
    if (!error.length)
        error = @"The build failed.";
    error = [[error substringToIndex:1].uppercaseString stringByAppendingString:[error substringFromIndex:1]];
    if ([text rangeOfString:@"operation not permitted" options:NSCaseInsensitiveSearch].location != NSNotFound)
        error = [error stringByAppendingString:@"\n\nIf macOS didn't allow Schedule I to access the files: System "
                                               @"Settings > Privacy & Security > Files & Folders > Schedule I."];
    return error;
}

// A panel drawn by the system, with up to three buttons (default, alternate, other)
static NSMutableDictionary *panel(NSString *header, NSString *message, NSArray<NSString *> *buttons)
{
    NSMutableDictionary *d = [NSMutableDictionary dictionary];
    d[(__bridge id)kCFUserNotificationAlertHeaderKey] = header;
    d[(__bridge id)kCFUserNotificationAlertMessageKey] = message;
    if (gIcon)
        d[(__bridge id)kCFUserNotificationIconURLKey] = gIcon;
    const CFStringRef keys[] = {kCFUserNotificationDefaultButtonTitleKey, kCFUserNotificationAlternateButtonTitleKey,
                                kCFUserNotificationOtherButtonTitleKey};
    for (NSUInteger i = 0; i < buttons.count && i < 3; i++)
        d[(__bridge id)keys[i]] = buttons[i];
    return d;
}

static NSDictionary *progressPanel(NSString *step)
{
    NSMutableDictionary *d = panel(@"Updating Schedule I",
                                   [NSString stringWithFormat:@"Schedule I was updated, so the Mac app is being rebuilt. "
                                                              @"The game starts when it's done.\n\n%@…", step],
                                   @[@"Stop"]);
    d[(__bridge id)kCFUserNotificationProgressIndicatorValueKey] = @YES;  // indeterminate
    return d;
}

// Shows a panel until a button is clicked: 0 for the default button, 1 for the alternate, 2 for the other one; 0
// when no panel can be shown (no GUI session). Quits if asked to.
static int ask(NSString *header, NSString *message, NSArray<NSString *> *buttons)
{
    SInt32 error = 0;
    CFUserNotificationRef note = CFUserNotificationCreate(NULL, 0, kCFUserNotificationCautionAlertLevel, &error,
                                                          (__bridge CFDictionaryRef)panel(header, message, buttons));
    if (!note)
        return 0;
    CFOptionFlags response = kCFUserNotificationDefaultResponse;
    while (CFUserNotificationReceiveResponse(note, 0.5, &response) != 0)
        if (gQuit)
        {
            CFUserNotificationCancel(note);
            exit(0);
        }
    CFRelease(note);
    return (int)(response & 3);
}

static void onQuitSignal(int sig)
{
    gQuit = 1;
}

// Interrupts the build, all its processes as Ctrl-C would, and waits for it to end (it cleans up first)
static void stopBuild(pid_t pid)
{
    kill(-pid, SIGINT);
    pid_t ended = 0;
    for (int i = 0; i < 300 && (ended = waitpid(pid, NULL, WNOHANG)) == 0; i++)
        usleep(100 * 1000);
    if (ended == 0)
    {
        kill(-pid, SIGKILL);
        waitpid(pid, NULL, 0);
    }
}

// Runs build.sh with its output going to log, showing a progress panel if it takes more than a moment, whose button
// stops the build. Returns the exit status, or -1 if stopped.
static int runBuild(NSString *script, NSArray<NSString *> *arguments, NSString *log)
{
    [NSFileManager.defaultManager createDirectoryAtPath:log.stringByDeletingLastPathComponent
                            withIntermediateDirectories:YES attributes:nil error:nil];
    NSArray<NSString *> *args = [@[script] arrayByAddingObjectsFromArray:arguments];
    char **argv = calloc(args.count + 1, sizeof(char *));
    for (NSUInteger i = 0; i < args.count; i++)
        argv[i] = (char *)args[i].UTF8String;
    posix_spawn_file_actions_t files;
    posix_spawn_file_actions_init(&files);
    posix_spawn_file_actions_addopen(&files, 0, "/dev/null", O_RDONLY, 0);
    posix_spawn_file_actions_addopen(&files, 1, log.fileSystemRepresentation, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    posix_spawn_file_actions_adddup2(&files, 1, 2);
    posix_spawnattr_t attributes;
    posix_spawnattr_init(&attributes);
    // its own process group, so stopBuild reaches all its processes
    posix_spawnattr_setflags(&attributes, POSIX_SPAWN_SETPGROUP | POSIX_SPAWN_SETSIGDEF | POSIX_SPAWN_CLOEXEC_DEFAULT);
    posix_spawnattr_setpgroup(&attributes, 0);
    sigset_t signals;
    sigemptyset(&signals);
    sigaddset(&signals, SIGINT);
    sigaddset(&signals, SIGTERM);
    sigaddset(&signals, SIGHUP);
    posix_spawnattr_setsigdefault(&attributes, &signals);
    pid_t pid;
    int error = posix_spawn(&pid, argv[0], &files, &attributes, argv, environ);
    posix_spawn_file_actions_destroy(&files);
    posix_spawnattr_destroy(&attributes);
    free(argv);
    if (error)
    {
        [[NSString stringWithFormat:@"error: couldn't run %@: %s\n", script, strerror(error)]
            writeToFile:log atomically:NO encoding:NSUTF8StringEncoding error:nil];
        return 1;
    }

    CFUserNotificationRef note = NULL;
    BOOL shown = NO;
    NSString *shownStep = nil;
    NSDate *started = [NSDate date];
    for (;;)
    {
        int status;
        if (waitpid(pid, &status, WNOHANG) == pid || gQuit)
        {
            if (note)
            {
                CFUserNotificationCancel(note);
                CFRelease(note);
            }
            if (gQuit)
            {
                stopBuild(pid);
                exit(0);
            }
            return WIFEXITED(status) ? WEXITSTATUS(status) : 128 + WTERMSIG(status);
        }
        NSString *step = buildStep(log);
        if (!shown && -started.timeIntervalSinceNow > 1)  // none for a build that fails right away
        {
            SInt32 err = 0;
            note = CFUserNotificationCreate(NULL, 0, kCFUserNotificationNoteAlertLevel, &err,
                                            (__bridge CFDictionaryRef)progressPanel(step));
            shown = YES;
            shownStep = step;
        }
        else if (note && ![step isEqualToString:shownStep])
        {
            CFUserNotificationUpdate(note, 0, kCFUserNotificationNoteAlertLevel,
                                     (__bridge CFDictionaryRef)progressPanel(step));
            shownStep = step;
        }
        CFOptionFlags response;
        if (!note)
            usleep(100 * 1000);
        else if (CFUserNotificationReceiveResponse(note, 0.5, &response) == 0)
        {
            CFRelease(note);
            note = NULL;
            if ((response & 3) == kCFUserNotificationDefaultResponse)  // Stop
            {
                stopBuild(pid);
                return -1;
            }
            shown = NO;  // closed some other way: shown again
        }
    }
}

static void showLog(NSString *log)
{
    char *argv[] = {"open", "-t", (char *)log.fileSystemRepresentation, NULL};
    pid_t pid;
    if (posix_spawn(&pid, "/usr/bin/open", NULL, NULL, argv, environ) == 0)
        waitpid(pid, NULL, 0);
}

// Rebuilds the app with ./build.sh if the game folder it was built from changed (MacPortUpdate in Info.plist, written
// by build.py). Returns to start the game as it is; after a build, the app at this path takes over the process.
static void updateIfNeeded(const char *argv[])
{
    NSBundle *bundle = NSBundle.mainBundle;
    NSDictionary *update = [bundle objectForInfoDictionaryKey:@"MacPortUpdate"];
    if (![update isKindOfClass:NSDictionary.class] || !changedGameFile(bundle, update[@"Game"]))
        return;  // unchanged, or e.g. the benchmark app
    NSString *app = bundle.bundlePath;
    NSString *script = buildScript(app, update[@"Repository"]);
    if (!script)
        return;  // the repository is gone, so it can't be rebuilt
    gIcon = [bundle URLForResource:@"PlayerIcon" withExtension:@"icns"];
    NSString *log = [script.stringByDeletingLastPathComponent stringByAppendingPathComponent:@"work/app-update.log"];
    NSMutableArray<NSString *> *arguments = [NSMutableArray array];
    if ([update[@"BuildArguments"] isKindOfClass:NSArray.class])
        [arguments addObjectsFromArray:update[@"BuildArguments"]];
    [arguments addObjectsFromArray:@[@"--game", update[@"Game"], @"--out", app]];
    struct sigaction quit = {.sa_handler = onQuitSignal};
    sigaction(SIGTERM, &quit, NULL);
    sigaction(SIGHUP, &quit, NULL);
    sigaction(SIGINT, &quit, NULL);
    // keeps macOS from slowing the build down (App Nap) while this process shows no window
    id activity = [NSProcessInfo.processInfo beginActivityWithOptions:NSActivityUserInitiated
                                                               reason:@"Rebuilding the app after a game update"];
    int status = runBuild(script, arguments, log);
    [NSProcessInfo.processInfo endActivity:activity];
    while (status != 0)
    {
        int choice;
        if (status < 0)
            choice = ask(@"Update stopped",
                         @"The app still has the previous version of the game. It updates the next time you open it.",
                         @[@"Play Anyway", @"Quit"]);
        else
            choice = ask(@"Couldn't update Schedule I",
                         [buildError(log) stringByAppendingString:@"\n\nYou can still play the previous version."],
                         @[@"Play Anyway", @"Quit", @"Show Log"]);
        if (choice == 0)
            break;
        if (choice != 2)
            exit(0);
        showLog(log);
    }
    // the rebuilt app, or this one if the build failed (it is only replaced once complete), takes over
    char exe[PATH_MAX];
    uint32_t size = sizeof exe;
    if (_NSGetExecutablePath(exe, &size) == 0)
    {
        setenv(kSkipCheck, "1", 1);
        execv(exe, (char *const *)argv);
    }
    ask(@"Schedule I couldn't be started", @(strerror(errno)), @[@"Quit"]);
    exit(1);
}

int main(int argc, const char *argv[])
{
    if (argc == 2 && strcmp(argv[1], "--check") == 0)
    {
        @autoreleasepool
        {
            NSBundle *bundle = NSBundle.mainBundle;
            NSDictionary *update = [bundle objectForInfoDictionaryKey:@"MacPortUpdate"];
            NSString *changed = [update isKindOfClass:NSDictionary.class] ? changedGameFile(bundle, update[@"Game"]) : nil;
            printf("%s\n", changed ? [@"changed: " stringByAppendingString:changed].UTF8String : "no change found");
            return changed ? 1 : 0;
        }
    }
    @autoreleasepool
    {
        if (getenv(kSkipCheck))
            unsetenv(kSkipCheck);
        else
            updateIfNeeded(argv);
        if (!PlayerMain)
        {
            ask(@"Schedule I couldn't be started",
                @"The app is incomplete (Frameworks/UnityPlayer.dylib is missing): run ./build.sh again.", @[@"Quit"]);
            return 1;
        }
    }
    return PlayerMain(argc, argv);
}
