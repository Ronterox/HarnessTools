"""C for analyze.py: one global namespace, headers paired with their .c, static / heuristic like the others.

CLang(Func) implements the hooks PyLang has (see analyze.py). Module names come from paths like JS's:
`src/render.c` -> `src.render`, `src/render.h` -> `src.render_h` (a header is its own module so it never
collides with its .c; it has no blob unless it defines inline / static functions, which then form the
header module's hub / orphan blob).

Resolution: a bare call `f(x)` is the file's own `f` first, else the one non-static definition of `f`
anywhere in the program (C links one global namespace; a header's static inline functions count as
visible to everyone). The name table is built per analysis in link(): every called / passed name is an
import `<module>.<name>` whose alias points at the definition, or `ext:<name>` for allowlisted libc /
POSIX / SDL / GL names and macro-looking names (ALL_CAPS, a `#define f(...)` of the file), which resolve
only when the program defines them and never count as unresolved.

Roots: `main` of the non-test module that defines it (kind `entry`, merged with the module's file-scope
code); a function only ever reached deferred from a file-scope table (`T[] = { on_a, ... }`,
`.fn = on_b`, `static void (*cb)(int) = on_c`) is a `hook` named after itself. Test modules (under
tests/ or test/, or named test_*.c / *_test.c) are one `tests` blob each. A program's one main() would hold
everything, so a big entry is split (split_entry): the functions main calls directly become `command` roots.
"""
import re, hashlib, posixpath, collections
from tree_sitter import Language, Parser
import tree_sitter_c as _tsc

_C = Parser(Language(_tsc.language()))
EXTS = ('.c', '.h')

# libc / POSIX / pthread / math names: calls to them are never the program's own (unless it defines them)
EXTERNAL = frozenset('''
printf fprintf sprintf snprintf vsnprintf vsprintf vfprintf vprintf dprintf vdprintf asprintf vasprintf puts fputs fputc
putc putchar fgets fgetc getc getchar ungetc fread fwrite fopen fdopen freopen fclose fflush fseek fseeko ftell ftello
rewind fileno popen pclose perror remove rename tmpfile tmpnam setvbuf setbuf sscanf fscanf scanf vsscanf getline getdelim
feof ferror clearerr open_memstream fmemopen flockfile funlockfile getc_unlocked putc_unlocked fgetpos fsetpos
malloc calloc realloc reallocarray free aligned_alloc posix_memalign memalign valloc exit _exit _Exit quick_exit abort
atexit at_quick_exit atoi atol atoll atof strtol strtoul strtoll strtoull strtod strtof strtold strtoimax strtoumax getenv
secure_getenv setenv unsetenv putenv clearenv system qsort qsort_r bsearch abs labs llabs div ldiv rand rand_r srand
random srandom drand48 lrand48 srand48 realpath mkstemp mkstemps mkdtemp mktemp arc4random getrandom
strlen strnlen strcpy strncpy strcat strncat strcmp strncmp strcoll strxfrm strcasecmp strncasecmp strchr strrchr
strchrnul strstr strcasestr strdup strndup strdupa strtok strtok_r strspn strcspn strpbrk strerror strerror_r strsignal
strsep stpcpy stpncpy strlcpy strlcat memcpy memmove memset memcmp memchr memrchr memmem mempcpy bzero bcopy explicit_bzero
basename dirname
isalpha isdigit isalnum isspace isupper islower isxdigit ispunct isprint iscntrl isgraph isblank isascii tolower toupper
iswalpha iswspace iswdigit towlower towupper wcwidth wcswidth mbtowc mbstowcs wcstombs mbrtowc wcrtomb mblen setlocale
sqrt sqrtf sqrtl pow powf exp expf exp2 exp2f expm1 log logf log2 log2f log10 log10f log1p fabs fabsf fabsl floor floorf
ceil ceilf round roundf lround lroundf llround rint rintf lrint lrintf nearbyint trunc truncf fmod fmodf remainder
sin sinf cos cosf tan tanf asin asinf acos acosf atan atanf atan2 atan2f sinh cosh tanh tanhf hypot hypotf cbrt cbrtf
fmin fminf fmax fmaxf fdim fma fmaf frexp ldexp ldexpf modf modff copysign copysignf signbit isnan isinf isfinite
isnormal fpclassify nan nanf erf erfc tgamma lgamma
time clock clock_gettime clock_getres clock_nanosleep nanosleep localtime localtime_r gmtime gmtime_r strftime strptime
mktime timegm difftime gettimeofday settimeofday ctime ctime_r asctime tzset usleep sleep alarm timer_create
timer_settime timer_delete
read write pread pwrite readv writev close lseek unlink unlinkat rmdir access faccessat getcwd chdir fchdir fork vfork
execvp execv execve execvpe execl execlp execle pipe pipe2 dup dup2 dup3 getpid getppid gettid isatty ttyname sysconf
pathconf readlink readlinkat symlink link ftruncate truncate fsync fdatasync sync getuid geteuid getgid getegid setsid
setpgid getpgrp chown fchown lchown gethostname getlogin getpagesize getopt getopt_long getopt_long_only
open openat creat fcntl ioctl flock lockf stat lstat fstat fstatat mkdir mkdirat chmod fchmod umask mkfifo mknod utime
utimes utimensat futimens statvfs fstatvfs
opendir fdopendir readdir readdir_r closedir rewinddir scandir alphasort dirfd nftw ftw glob globfree fnmatch wordexp
wordfree
signal sigaction raise kill killpg sigemptyset sigfillset sigaddset sigdelset sigismember sigprocmask sigsuspend
sigwait sigwaitinfo pause psignal strsignal
wait waitpid waitid wait3 wait4 poll ppoll select pselect
socket socketpair bind listen accept accept4 connect send recv sendto recvfrom sendmsg recvmsg setsockopt getsockopt
shutdown getaddrinfo freeaddrinfo gai_strerror getnameinfo gethostbyname getpeername getsockname inet_pton inet_ntop
inet_addr inet_ntoa htons htonl ntohs ntohl
mmap munmap mprotect msync madvise mlock munlock shm_open shm_unlink memfd_create
dlopen dlsym dlclose dlerror dladdr
regcomp regexec regfree regerror
assert va_start va_end va_arg va_copy setjmp longjmp sigsetjmp siglongjmp offsetof alloca
err errx warn warnx verr verrx vwarn vwarnx syslog openlog closelog
iconv iconv_open iconv_close uselocale newlocale freelocale duplocale nl_langinfo
getpwuid getpwnam getpwuid_r getpwnam_r getgrgid_r getgrnam_r getgrgid getgrnam getrlimit setrlimit getrusage prctl uname sched_yield
sched_getaffinity sched_setaffinity
'''.split())
# whole families by prefix: pthread_, sem_, epoll_, inotify_, posix_, __builtin_, atomics, SDL / GL / GLFW / rlgl
EXTERNAL_PREFIX = re.compile(r'(?:pthread_|sem_|epoll_|inotify_|eventfd|timerfd_|signalfd|posix_|__builtin_|__sync_|'
                             r'__atomic_|atomic_|SDL_|IMG_|TTF_|Mix_|gl[A-Z]|glu[A-Z]|glfw[A-Z]|glew[A-Z]|egl[A-Z]|'
                             r'rl[A-Z]|al[A-Z]|vk[A-Z])')
# libc globals a function reads that are not program state
EXTERNAL_DATA = frozenset({'stdout', 'stderr', 'stdin', 'errno', 'optarg', 'optind', 'opterr', 'optopt', 'environ',
                           '__func__', '__FUNCTION__', '__PRETTY_FUNCTION__'})
# What a header missing from the snapshot (a vendored or system library) provides: a name it would declare is
# external, not unresolved, in a module that includes it (directly or through the program's own headers).
# Bundled lists for headers with no common prefix (raylib 5.5 RLAPI / raymath RMAPI); else HEADER_PREFIXES,
# else the header's stem + '_' (yyjson.h -> yyjson_*, sqlite3.h -> sqlite3_*).
RAYLIB = frozenset('''
AttachAudioMixedProcessor AttachAudioStreamProcessor BeginBlendMode BeginDrawing BeginMode2D BeginMode3D
BeginScissorMode BeginShaderMode BeginTextureMode BeginVrStereoMode ChangeDirectory CheckCollisionBoxes
CheckCollisionBoxSphere CheckCollisionCircleLine CheckCollisionCircleRec CheckCollisionCircles CheckCollisionLines
CheckCollisionPointCircle CheckCollisionPointLine CheckCollisionPointPoly CheckCollisionPointRec
CheckCollisionPointTriangle CheckCollisionRecs CheckCollisionSpheres ClearBackground ClearWindowState
CloseAudioDevice CloseWindow CodepointToUTF8 ColorAlpha ColorAlphaBlend ColorBrightness ColorContrast ColorFromHSV
ColorFromNormalized ColorIsEqual ColorLerp ColorNormalize ColorTint ColorToHSV ColorToInt CompressData ComputeCRC32
ComputeMD5 ComputeSHA1 DecodeDataBase64 DecompressData DetachAudioMixedProcessor DetachAudioStreamProcessor
DirectoryExists DisableCursor DisableEventWaiting DrawBillboard DrawBillboardPro DrawBillboardRec DrawBoundingBox
DrawCapsule DrawCapsuleWires DrawCircle DrawCircle3D DrawCircleGradient DrawCircleLines DrawCircleLinesV
DrawCircleSector DrawCircleSectorLines DrawCircleV DrawCube DrawCubeV DrawCubeWires DrawCubeWiresV DrawCylinder
DrawCylinderEx DrawCylinderWires DrawCylinderWiresEx DrawEllipse DrawEllipseLines DrawFPS DrawGrid DrawLine
DrawLine3D DrawLineBezier DrawLineEx DrawLineStrip DrawLineV DrawMesh DrawMeshInstanced DrawModel DrawModelEx
DrawModelPoints DrawModelPointsEx DrawModelWires DrawModelWiresEx DrawPixel DrawPixelV DrawPlane DrawPoint3D
DrawPoly DrawPolyLines DrawPolyLinesEx DrawRay DrawRectangle DrawRectangleGradientEx DrawRectangleGradientH
DrawRectangleGradientV DrawRectangleLines DrawRectangleLinesEx DrawRectanglePro DrawRectangleRec
DrawRectangleRounded DrawRectangleRoundedLines DrawRectangleRoundedLinesEx DrawRectangleV DrawRing DrawRingLines
DrawSphere DrawSphereEx DrawSphereWires DrawSplineBasis DrawSplineBezierCubic DrawSplineBezierQuadratic
DrawSplineCatmullRom DrawSplineLinear DrawSplineSegmentBasis DrawSplineSegmentBezierCubic
DrawSplineSegmentBezierQuadratic DrawSplineSegmentCatmullRom DrawSplineSegmentLinear DrawText DrawTextCodepoint
DrawTextCodepoints DrawTextEx DrawTextPro DrawTexture DrawTextureEx DrawTextureNPatch DrawTexturePro DrawTextureRec
DrawTextureV DrawTriangle DrawTriangle3D DrawTriangleFan DrawTriangleLines DrawTriangleStrip DrawTriangleStrip3D
EnableCursor EnableEventWaiting EncodeDataBase64 EndBlendMode EndDrawing EndMode2D EndMode3D EndScissorMode
EndShaderMode EndTextureMode EndVrStereoMode ExportAutomationEventList ExportDataAsCode ExportFontAsCode
ExportImage ExportImageAsCode ExportImageToMemory ExportMesh ExportMeshAsCode ExportWave ExportWaveAsCode Fade
FileExists GenImageCellular GenImageChecked GenImageColor GenImageFontAtlas GenImageGradientLinear
GenImageGradientRadial GenImageGradientSquare GenImagePerlinNoise GenImageText GenImageWhiteNoise GenMeshCone
GenMeshCube GenMeshCubicmap GenMeshCylinder GenMeshHeightmap GenMeshHemiSphere GenMeshKnot GenMeshPlane GenMeshPoly
GenMeshSphere GenMeshTangents GenMeshTorus GenTextureMipmaps GetApplicationDirectory GetCameraMatrix
GetCameraMatrix2D GetCharPressed GetClipboardImage GetClipboardText GetCodepoint GetCodepointCount GetCodepointNext
GetCodepointPrevious GetCollisionRec GetColor GetCurrentMonitor GetDirectoryPath GetFileExtension GetFileLength
GetFileModTime GetFileName GetFileNameWithoutExt GetFontDefault GetFPS GetFrameTime GetGamepadAxisCount
GetGamepadAxisMovement GetGamepadButtonPressed GetGamepadName GetGestureDetected GetGestureDragAngle
GetGestureDragVector GetGestureHoldDuration GetGesturePinchAngle GetGesturePinchVector GetGlyphAtlasRec
GetGlyphIndex GetGlyphInfo GetImageAlphaBorder GetImageColor GetKeyPressed GetMasterVolume GetMeshBoundingBox
GetModelBoundingBox GetMonitorCount GetMonitorHeight GetMonitorName GetMonitorPhysicalHeight
GetMonitorPhysicalWidth GetMonitorPosition GetMonitorRefreshRate GetMonitorWidth GetMouseDelta GetMousePosition
GetMouseWheelMove GetMouseWheelMoveV GetMouseX GetMouseY GetMusicTimeLength GetMusicTimePlayed GetPixelColor
GetPixelDataSize GetPrevDirectoryPath GetRandomValue GetRayCollisionBox GetRayCollisionMesh GetRayCollisionQuad
GetRayCollisionSphere GetRayCollisionTriangle GetRenderHeight GetRenderWidth GetScreenHeight GetScreenToWorld2D
GetScreenToWorldRay GetScreenToWorldRayEx GetScreenWidth GetShaderLocation GetShaderLocationAttrib GetShapesTexture
GetShapesTextureRectangle GetSplinePointBasis GetSplinePointBezierCubic GetSplinePointBezierQuad
GetSplinePointCatmullRom GetSplinePointLinear GetTime GetTouchPointCount GetTouchPointId GetTouchPosition GetTouchX
GetTouchY GetWindowHandle GetWindowPosition GetWindowScaleDPI GetWorkingDirectory GetWorldToScreen
GetWorldToScreen2D GetWorldToScreenEx HideCursor ImageAlphaClear ImageAlphaCrop ImageAlphaMask
ImageAlphaPremultiply ImageBlurGaussian ImageClearBackground ImageColorBrightness ImageColorContrast
ImageColorGrayscale ImageColorInvert ImageColorReplace ImageColorTint ImageCopy ImageCrop ImageDither ImageDraw
ImageDrawCircle ImageDrawCircleLines ImageDrawCircleLinesV ImageDrawCircleV ImageDrawLine ImageDrawLineEx
ImageDrawLineV ImageDrawPixel ImageDrawPixelV ImageDrawRectangle ImageDrawRectangleLines ImageDrawRectangleRec
ImageDrawRectangleV ImageDrawText ImageDrawTextEx ImageDrawTriangle ImageDrawTriangleEx ImageDrawTriangleFan
ImageDrawTriangleLines ImageDrawTriangleStrip ImageFlipHorizontal ImageFlipVertical ImageFormat ImageFromChannel
ImageFromImage ImageKernelConvolution ImageMipmaps ImageResize ImageResizeCanvas ImageResizeNN ImageRotate
ImageRotateCCW ImageRotateCW ImageText ImageTextEx ImageToPOT InitAudioDevice InitWindow IsAudioDeviceReady
IsAudioStreamPlaying IsAudioStreamProcessed IsAudioStreamValid IsCursorHidden IsCursorOnScreen IsFileDropped
IsFileExtension IsFileNameValid IsFontValid IsGamepadAvailable IsGamepadButtonDown IsGamepadButtonPressed
IsGamepadButtonReleased IsGamepadButtonUp IsGestureDetected IsImageValid IsKeyDown IsKeyPressed IsKeyPressedRepeat
IsKeyReleased IsKeyUp IsMaterialValid IsModelAnimationValid IsModelValid IsMouseButtonDown IsMouseButtonPressed
IsMouseButtonReleased IsMouseButtonUp IsMusicStreamPlaying IsMusicValid IsPathFile IsRenderTextureValid
IsShaderValid IsSoundPlaying IsSoundValid IsTextureValid IsWaveValid IsWindowFocused IsWindowFullscreen
IsWindowHidden IsWindowMaximized IsWindowMinimized IsWindowReady IsWindowResized IsWindowState LoadAudioStream
LoadAutomationEventList LoadCodepoints LoadDirectoryFiles LoadDirectoryFilesEx LoadDroppedFiles LoadFileData
LoadFileText LoadFont LoadFontData LoadFontEx LoadFontFromImage LoadFontFromMemory LoadImage LoadImageAnim
LoadImageAnimFromMemory LoadImageColors LoadImageFromMemory LoadImageFromScreen LoadImageFromTexture
LoadImagePalette LoadImageRaw LoadMaterialDefault LoadMaterials LoadModel LoadModelAnimations LoadModelFromMesh
LoadMusicStream LoadMusicStreamFromMemory LoadRandomSequence LoadRenderTexture LoadShader LoadShaderFromMemory
LoadSound LoadSoundAlias LoadSoundFromWave LoadTexture LoadTextureCubemap LoadTextureFromImage LoadUTF8
LoadVrStereoConfig LoadWave LoadWaveFromMemory LoadWaveSamples MakeDirectory MaximizeWindow MeasureText
MeasureTextEx MemAlloc MemFree MemRealloc MinimizeWindow OpenURL PauseAudioStream PauseMusicStream PauseSound
PlayAudioStream PlayAutomationEvent PlayMusicStream PlaySound PollInputEvents RestoreWindow ResumeAudioStream
ResumeMusicStream ResumeSound SaveFileData SaveFileText SeekMusicStream SetAudioStreamBufferSizeDefault
SetAudioStreamCallback SetAudioStreamPan SetAudioStreamPitch SetAudioStreamVolume SetAutomationEventBaseFrame
SetAutomationEventList SetClipboardText SetConfigFlags SetExitKey SetGamepadMappings SetGamepadVibration
SetGesturesEnabled SetLoadFileDataCallback SetLoadFileTextCallback SetMasterVolume SetMaterialTexture
SetModelMeshMaterial SetMouseCursor SetMouseOffset SetMousePosition SetMouseScale SetMusicPan SetMusicPitch
SetMusicVolume SetPixelColor SetRandomSeed SetSaveFileDataCallback SetSaveFileTextCallback SetShaderValue
SetShaderValueMatrix SetShaderValueTexture SetShaderValueV SetShapesTexture SetSoundPan SetSoundPitch
SetSoundVolume SetTargetFPS SetTextLineSpacing SetTextureFilter SetTextureWrap SetTraceLogCallback SetTraceLogLevel
SetWindowFocused SetWindowIcon SetWindowIcons SetWindowMaxSize SetWindowMinSize SetWindowMonitor SetWindowOpacity
SetWindowPosition SetWindowSize SetWindowState SetWindowTitle ShowCursor StartAutomationEventRecording
StopAudioStream StopAutomationEventRecording StopMusicStream StopSound SwapScreenBuffer TakeScreenshot TextAppend
TextCopy TextFindIndex TextFormat TextInsert TextIsEqual TextJoin TextLength TextReplace TextSplit TextSubtext
TextToCamel TextToFloat TextToInteger TextToLower TextToPascal TextToSnake TextToUpper ToggleBorderlessWindowed
ToggleFullscreen TraceLog UnloadAudioStream UnloadAutomationEventList UnloadCodepoints UnloadDirectoryFiles
UnloadDroppedFiles UnloadFileData UnloadFileText UnloadFont UnloadFontData UnloadImage UnloadImageColors
UnloadImagePalette UnloadMaterial UnloadMesh UnloadModel UnloadModelAnimation UnloadModelAnimations
UnloadMusicStream UnloadRandomSequence UnloadRenderTexture UnloadShader UnloadSound UnloadSoundAlias UnloadTexture
UnloadUTF8 UnloadVrStereoConfig UnloadWave UnloadWaveSamples UpdateAudioStream UpdateCamera UpdateCameraPro
UpdateMeshBuffer UpdateModelAnimation UpdateModelAnimationBones UpdateMusicStream UpdateSound UpdateTexture
UpdateTextureRec UploadMesh WaitTime WaveCopy WaveCrop WaveFormat WindowShouldClose
'''.split())
RAYMATH = frozenset('''
Clamp FloatEquals Lerp MatrixAdd MatrixDecompose MatrixDeterminant MatrixFrustum MatrixIdentity MatrixInvert
MatrixLookAt MatrixMultiply MatrixOrtho MatrixPerspective MatrixRotate MatrixRotateX MatrixRotateXYZ MatrixRotateY
MatrixRotateZ MatrixRotateZYX MatrixScale MatrixSubtract MatrixToFloatV MatrixTrace MatrixTranslate MatrixTranspose
Normalize QuaternionAdd QuaternionAddValue QuaternionCubicHermiteSpline QuaternionDivide QuaternionEquals
QuaternionFromAxisAngle QuaternionFromEuler QuaternionFromMatrix QuaternionFromVector3ToVector3 QuaternionIdentity
QuaternionInvert QuaternionLength QuaternionLerp QuaternionMultiply QuaternionNlerp QuaternionNormalize
QuaternionScale QuaternionSlerp QuaternionSubtract QuaternionSubtractValue QuaternionToAxisAngle QuaternionToEuler
QuaternionToMatrix QuaternionTransform Remap Vector2Add Vector2AddValue Vector2Angle Vector2Clamp Vector2ClampValue
Vector2Distance Vector2DistanceSqr Vector2Divide Vector2DotProduct Vector2Equals Vector2Invert Vector2Length
Vector2LengthSqr Vector2Lerp Vector2LineAngle Vector2Max Vector2Min Vector2MoveTowards Vector2Multiply
Vector2Negate Vector2Normalize Vector2One Vector2Reflect Vector2Refract Vector2Rotate Vector2Scale Vector2Subtract
Vector2SubtractValue Vector2Transform Vector2Zero Vector3Add Vector3AddValue Vector3Angle Vector3Barycenter
Vector3Clamp Vector3ClampValue Vector3CrossProduct Vector3CubicHermite Vector3Distance Vector3DistanceSqr
Vector3Divide Vector3DotProduct Vector3Equals Vector3Invert Vector3Length Vector3LengthSqr Vector3Lerp Vector3Max
Vector3Min Vector3MoveTowards Vector3Multiply Vector3Negate Vector3Normalize Vector3One Vector3OrthoNormalize
Vector3Perpendicular Vector3Project Vector3Reflect Vector3Refract Vector3Reject Vector3RotateByAxisAngle
Vector3RotateByQuaternion Vector3Scale Vector3Subtract Vector3SubtractValue Vector3ToFloatV Vector3Transform
Vector3Unproject Vector3Zero Vector4Add Vector4AddValue Vector4Distance Vector4DistanceSqr Vector4Divide
Vector4DotProduct Vector4Equals Vector4Invert Vector4Length Vector4LengthSqr Vector4Lerp Vector4Max Vector4Min
Vector4MoveTowards Vector4Multiply Vector4Negate Vector4Normalize Vector4One Vector4Scale Vector4Subtract
Vector4SubtractValue Vector4Zero Wrap
'''.split())
HEADER_NAMES = {'raylib.h': RAYLIB, 'raymath.h': RAYMATH}
HEADER_PREFIXES = {'tree_sitter/api.h': ('ts_', 'tree_sitter_'), 'rlgl.h': ('rl',)}
EXIT_CALLS = frozenset({'exit', '_exit', '_Exit', 'quick_exit', 'abort', 'err', 'errx', 'verr', 'verrx',
                        'die', 'fatal', 'panic'})
ERROR_CALLEE = re.compile(r'(?:\w+_)?(?:die|fatal|panic|error|err|fail|warn|warnx|errx)|perror|abort|_?exit')
ERR_LABEL = re.compile(r'(?:\w+_)?(?:fail|failed|failure|err|error|errout|bail|abort|die)\w*', re.I)
CAPS = re.compile(r'_*[A-Z][A-Z0-9_]*')
FILE_CALLS = {'fopen': 0, 'fopen64': 0, 'freopen': 0, 'open': 0, 'open64': 0, 'creat': 0, 'opendir': 0, 'openat': 1,
              'popen': None}
ENV_CALLS = frozenset({'getenv', 'secure_getenv', 'setenv', 'unsetenv'})
BRANCH_TYPES = {'if_statement', 'for_statement', 'while_statement', 'do_statement', 'case_statement',
                'conditional_expression'}
SKIP_TYPES = {'comment', 'preproc_def', 'preproc_function_def', 'preproc_include', 'preproc_call', 'type_descriptor',
              'primitive_type', 'type_identifier', 'sized_type_specifier', 'struct_specifier', 'union_specifier',
              'enum_specifier', 'type_definition', 'field_identifier', 'statement_identifier', 'gnu_asm_expression'}
NESTING = {'preproc_if', 'preproc_ifdef', 'preproc_else', 'preproc_elif', 'preproc_elifdef', 'linkage_specification',
           'declaration_list', 'ERROR'}
DECL_WRAPPERS = ('pointer_declarator', 'attributed_declarator', 'array_declarator', 'parenthesized_declarator',
                 'function_declarator', 'init_declarator')
SQL = re.compile(r'\b(?:FROM|INTO|UPDATE|TABLE(?: IF NOT EXISTS)?|JOIN)\s+([A-Za-z_][A-Za-z_0-9]*)', re.I)
SQL_STMT = re.compile(r'\s*(?:SELECT|INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|REPLACE|WITH)\s')  # prose is not SQL
# externals whose argument at this index is a function: never a data symbol, even when defined in another file
CALLBACK_ARGS = {'qsort': 3, 'qsort_r': 3, 'bsearch': 4, 'lfind': 4, 'lsearch': 4, 'tsearch': 2, 'tfind': 2,
                 'pthread_create': 2, 'pthread_once': 1, 'thrd_create': 1, 'signal': 1, 'atexit': 0,
                 'at_quick_exit': 0, 'on_exit': 0, 'scandir': 2, 'nftw': 1, 'ftw': 1, 'pthread_atfork': 0}
STR = re.compile(r'(?:L|u8|u|U)?"(.*)"', re.S)
PATH = re.compile(r'[\w.@+~-]+(?:/[\w.@+~-]+)*/?')
INCLUDE_DIRS = ('', 'src/', 'include/')

class _External:
    """The allowlist as analyze() asks it (`name in L.builtins`): EXTERNAL plus the EXTERNAL_PREFIX families."""
    def __contains__(self, name):
        return name in EXTERNAL or bool(EXTERNAL_PREFIX.match(name))

def external(name):
    return name in EXTERNAL or bool(EXTERNAL_PREFIX.match(name))

def provides(spec, name):
    """Would the include `spec` ("raylib.h", "tree_sitter/api.h") declare `name`?"""
    base = spec.rsplit('/', 1)[-1]
    if spec in HEADER_NAMES or base in HEADER_NAMES:
        return name in HEADER_NAMES.get(spec, HEADER_NAMES.get(base))
    pre = HEADER_PREFIXES.get(spec) or HEADER_PREFIXES.get(base) or (posixpath.splitext(base)[0] + '_',)
    return name.startswith(pre)

def include_paths(rel, spec):
    """Snapshot paths `#include "spec"` in file rel may name: next to rel, then the root, src/ and include/."""
    out = []
    for cand in [posixpath.join(posixpath.dirname(rel), spec)] + [d + spec for d in INCLUDE_DIRS]:
        cand = posixpath.normpath(cand)
        if not cand.startswith('..') and cand not in out:
            out.append(cand)
    return out

def _text(n, src):
    return src[n.start_byte:n.end_byte].decode('utf-8', 'replace')

def _hash(s):
    return hashlib.sha1(s.encode()).hexdigest()[:10]

def _norm(callee):
    """Callee text as the resolver reads it: no whitespace, `->` is `.`, `(*fp)` / `(fp)` is `fp`."""
    c = re.sub(r'\s+', '', callee).replace('->', '.')
    m = re.fullmatch(r'\(\**([A-Za-z_][\w.]*)\)', c)
    return m.group(1) if m else c

def _strval(n, src):
    if n is None or n.type != 'string_literal':
        return None
    m = STR.fullmatch(_text(n, src))
    return m.group(1) if m else None

def _declname(d, src):
    """The identifier a (possibly pointer / array / function / init) declarator declares, or None."""
    while d is not None and d.type in DECL_WRAPPERS:
        nxt = d.child_by_field_name('declarator')
        if nxt is None:
            nxt = next((c for c in d.named_children if c.type in DECL_WRAPPERS + ('identifier',)), None)
        d = nxt
    return _text(d, src) if d is not None and d.type == 'identifier' else None

def _is_proto(d):
    """`int f(int)` / `char *f(void)`: a function declarator naming a function (not `(*fp)(int)`, a variable)."""
    while d is not None and d.type in ('pointer_declarator', 'attributed_declarator'):
        d = d.child_by_field_name('declarator')
    return d is not None and d.type == 'function_declarator' and \
        d.child_by_field_name('declarator') is not None and d.child_by_field_name('declarator').type == 'identifier'

def _storage(n, src):
    return {_text(c, src) for c in n.children if c.type == 'storage_class_specifier'}

TYPEOF = re.compile(rb'\b(?:__typeof__|__typeof|typeof)\s*\(')
LEXEME = re.compile(rb'''//[^\n]*|/\*.*?(?:\*/|\Z)|"(?:\\.|[^"\\\n])*"?|'(?:\\.|[^'\\\n])*'?''', re.S)   # comments, literals

def _sanitize(src):
    """GNU `typeof(expr)` -> a same-length plain type name, so `(typeof(x)){...}` compound literals parse
    (tree-sitter-c's recovery otherwise folds the rest of the file into one function). Byte offsets keep.
    Only code counts: comments and string / char literals are masked first, and a match whose parenthesis
    does not close before a `;` or `{` at depth 1 (or the end of the file) is left alone."""
    if not TYPEOF.search(src):
        return src
    code = bytearray(src)
    for m in LEXEME.finditer(src):
        for i in range(m.start(), m.end()):
            if code[i] != 10:
                code[i] = 32
    code = bytes(code)
    out, pos = bytearray(), 0
    for m in TYPEOF.finditer(code):
        if m.start() < pos:
            continue
        depth, i = 1, m.end()
        while i < len(code) and depth:
            c = code[i]
            if depth == 1 and c in b';{':
                break
            depth += {40: 1, 41: -1}.get(c, 0)
            i += 1
        if depth:
            continue
        span = src[m.start():i]
        out += src[pos:m.start()] + b'int' + bytes(c if c in b'\n' else 32 for c in span[3:])
        pos = i
    return bytes(out + src[pos:]) if pos else src

def modname(rel):
    stem, ext = posixpath.splitext(rel)
    parts = stem.split('/')
    parts[-1] += '_h' if ext == '.h' else ''
    return '.'.join(p.replace('.', '_') for p in parts)

class CLang:
    name, ctor, module_types = 'c', '__init__', False
    builtins = stdlib = _External()

    def __init__(self, Func):
        self.Func = Func

    def handles(self, rel):
        return rel.endswith(EXTS)

    def modname(self, rel):
        return modname(rel)

    def is_test(self, mod, rel):
        parts = rel.split('/')
        stem = posixpath.splitext(parts[-1])[0]
        return bool({'test', 'tests'} & set(parts[:-1])) or stem.startswith('test_') or stem.endswith('_test')

    def parse_module(self, rel, text):
        return _Module(self, rel, text).info

    # ------------------------------------------------------------ analysis hooks

    def link(self, mods, funcs, class_keys):
        """alias: `<module>.<name>` / `ext:<name>` import targets -> the definition they name (see the module doc),
        or -> `ext:<name>` when nothing defines it and a header missing from the snapshot would (analyze() then does
        not count the call as unresolved); hooks: quals only ever referenced from file-scope tables."""
        test = {m: self.is_test(m, info['file']) for m, info in mods.items()}
        defs = collections.defaultdict(list)
        for m, info in mods.items():
            for f in info['funcs']:
                if not getattr(f, 'static', False) or info['file'].endswith('.h'):
                    defs[f.name].append(f)
        for fs in defs.values():
            fs.sort(key=lambda f: (test[f.module], f.file.endswith('.h'), f.module))
        picked = {}

        def pick(name, m):
            """The definition a call to `name` from module m links to: non-test before test (a test module only),
            .c before header, same directory first."""
            key = (name, m is not None and test[m], m.rsplit('.', 1)[0] if m and '.' in m else '')
            if key not in picked:
                cands = [f for f in defs.get(name, ()) if key[1] or not test[f.module]]
                same = [f for f in cands if (f.module.rsplit('.', 1)[0] if '.' in f.module else '') == key[2]
                        and (test[f.module], f.file.endswith('.h')) == (test[cands[0].module], cands[0].file.endswith('.h'))]
                picked[key] = (same or cands or [None])[0]
            f = picked[key]
            return f.qual if f is not None else None

        files = {info['file']: m for m, info in mods.items()}
        missing_of = {}

        def missing(m):
            """Includes of m, directly or through the program's own headers, that are not in the snapshot."""
            if m not in missing_of:
                missing_of[m] = out = set()
                for spec in mods[m].get('includes', ()):
                    hit = next((files[c] for c in include_paths(mods[m]['file'], spec) if c in files), None)
                    out.update(missing(hit) if hit is not None else (spec,))
            return missing_of[m]

        alias = {}
        for m, info in mods.items():
            for name, tgt in info['imports'].items():
                if tgt in alias or tgt in funcs:
                    continue
                q = pick(name, None if tgt.startswith('ext:') else m)
                if q:
                    alias[tgt] = q
                elif not tgt.startswith('ext:') and any(provides(spec, name) for spec in sorted(missing(m))):
                    alias[tgt] = 'ext:' + name     # a vendored / system library's: external, not unresolved

        def res(m, name):
            q = f'{m}.{name}'
            if q in funcs:
                return q
            q = alias.get(mods[m]['imports'].get(name))
            return q if q in funcs else None

        table = {}                          # qual -> True: referenced from a file-scope initializer
        for m, info in mods.items():
            top = info.get('top')
            if top is None or test[m]:
                continue
            for callee, _, how in top.calls:
                if how == 'deferred':
                    q = res(m, callee)
                    if q:
                        table[q] = True
        hooks = set()
        if table:
            names = {funcs[q].name for q in table}
            used = set()
            for m, info in mods.items():
                for f in info['funcs']:
                    for callee, _, _ in f.calls:
                        if callee in names and callee not in f.locals:
                            used.add(res(m, callee))
            hooks = {q for q in table if q not in used and funcs[q].name != 'main'}
        return {'alias': alias, 'hooks': hooks}

    def entry(self, ms, mods, imported, closure, is_test, files):
        """The non-test module that defines main(); several: the one whose includes reach the most modules."""
        cands = [m for m in ms if not is_test(m) and mods[m].get('has_main')]
        return max(cands, key=lambda m: (len(closure(m)), m.split('.')[-1] == 'main'), default=None)

    def route_of(self, f, extra, ctx):
        return (f.name, 'hook') if f.qual in ctx.get('hooks', ()) else None

    def snippet(self, f):
        """Up to 3 lines: error exits and effects first (return -1 / NULL / false, exit, abort, goto, stderr,
        file writes), then other returns, then the rest minus bare declarations and preprocessor lines."""
        lines, body = [], f.body.splitlines()
        if f.name != '<module>':           # drop the signature: everything up to the body's `{`
            for i, raw in enumerate(body):
                if '{' in raw:
                    body = [raw.split('{', 1)[1]] + body[i + 1:]
                    break
        in_comment = False
        for raw in body:
            l = raw.strip()
            if in_comment:
                in_comment = '*/' not in l
                continue
            if l.startswith('/*') and '*/' not in l:
                in_comment = True
                continue
            if not l or re.fullmatch(r'[{}()\[\];,]+', l) or l.startswith(('//', '/*', '*', '#')):
                continue
            lines.append(l)
        def tier(l):
            if re.search(r'\breturn\s+(-|NULL\b|false\b)|\b(exit|_exit|abort|die|fatal|panic)\s*\(|\bgoto\b|'
                         r'\bfprintf\s*\(\s*stderr|\bperror\s*\(|\b(fopen|fwrite|write|unlink|rename|send)\s*\(', l):
                return 0
            if re.match(r'return\b', l):
                return 1
            return 3 if re.fullmatch(r'(?:(?:static|const|unsigned|signed|struct|enum|union)\s+)*\w+[\s*]+\w+(\[[^\]]*\])?;', l) \
                else 2
        best = sorted(range(len(lines)), key=lambda i: (tier(lines[i]), i))
        return [lines[i] for i in sorted(i for i in best[:3] if tier(lines[i]) < 3)]

    def extra_refs(self, f, others, ctx):
        return ()

    # split_entry(): an entry that alone reaches split_min functions or more is cut at main's direct callees;
    # when one of them owns more than split_share of the entry, at that one's callees instead
    split_min, split_share = 20, 0.5

    def split_entry(self, entry, roots, callgraph, funcs, is_test):
        """The `command` roots a big entry root (blob id, name, kind, [quals]) splits into, and the depth they sit
        at (1: main's callees, 2: the callees of the main callee that owns more than split_share of the entry; owns:
        reaches while no other callee does and main does not except through it), or ([], None).
        A callee qualifies unless it is a root already, a test's, shorter than 3 lines (unless main's callee owns more
        than split_share: a `run() { loop(); }` wrapper) or called by 2+ functions other than main (and the depth-2
        parent): a helper. Everything else stays as analyze() places it: main
        keeps what only it reaches, what several commands reach goes to the hubs."""
        starts = entry[3]
        main = next((q for q in starts if funcs[q].name == 'main'), None)
        if main is None:
            return [], None
        taken = {q for r in roots if r[2] != 'tests' for q in r[3]}

        def walk(qs, stop):
            seen, stack = set(qs), list(qs)
            while stack:
                for r, *_ in callgraph.get(stack.pop(), ()):
                    if r not in seen and r not in stop:
                        seen.add(r)
                        stack.append(r)
            return seen

        others = set()
        for r in roots:
            if r is not entry and r[2] != 'tests':
                others |= walk(r[3], taken)
        mine = walk(starts, taken) - others            # what the entry blob would hold
        if len(mine) < self.split_min:
            return [], None
        callers = collections.defaultdict(set)
        for q, cs in callgraph.items():
            if not is_test(funcs[q].module):
                for r, *_ in cs:
                    callers[r].add(q)

        def picks(parent, skip, short=False):
            """parent's qualifying callees; short: 1-2 line ones too (the caller decides)."""
            out = []
            for r, *_ in callgraph.get(parent, ()):
                f = funcs[r]
                if r in out or r in skip or r in taken or is_test(f.module) or f.name == '<module>' or \
                        (f.lines < 3 and not short) or len(callers[r] - skip) >= 2:
                    continue
                out.append(r)
            return out

        cands, depth = picks(main, set(starts), short=True), 1
        if cands:
            reach = {c: walk([c], taken | set(cands) - {c}) & mine for c in cands}
            rest = walk(starts, taken | set(cands)) & mine
            owned = {c: reach[c] - rest - set().union(*(reach[o] for o in cands if o != c)) for c in cands}
            big = max(cands, key=lambda c: len(owned[c]))
            if len(owned[big]) <= self.split_share * len(mine):
                big = None
            cands = [c for c in cands if c == big or funcs[c].lines >= 3]
            if big:
                sub = [r for r in picks(big, set(starts) | {big}) if r not in cands]
                if sub:
                    cands, depth = [c for c in cands if c != big] + sub, 2
        if not cands:
            return [], None
        return [(q, funcs[q].name, 'command', [q]) for q in cands], depth

class _Module:
    """One parse: the module info dict analyze() expects, plus has_main / includes."""

    def __init__(self, lang, rel, text):
        self.Func, self.rel = lang.Func, rel
        self.src = src = text.encode('utf-8')
        root = _C.parse(_sanitize(src)).root_node
        self.mod = mod = modname(rel)
        self.info = info = {'name': mod, 'file': rel, 'imports': {}, 'funcs': [], 'globals': set(), 'classes': set(),
                            'blueprints': set(), 'framework': set(), 'lines': text.count('\n') + 1,
                            'entry_like': False, 'deps': [], 'global_tables': {}, 'class_attrs': {},
                            'includes': [], 'has_main': False}
        self.fdefs, self.decls = [], []
        self.vars = {}            # file-scope variable -> declared static
        self.macros = set()       # function-like macros defined here
        self.enums = set()        # enumerators defined here (constants, not data)
        self.collect(root)
        self.fnames = set()
        for n in self.fdefs:
            name = _declname(n.child_by_field_name('declarator'), src)
            if name:
                self.fnames.add(name)
        self.called = set()       # bare names called anywhere in the file: functions, not data
        top = self.Func(mod, '<module>', root, src)
        self.top = top
        for d in self.decls:
            self.walk(d, top, False)
        seen = set()
        for n in self.fdefs:
            name = _declname(n.child_by_field_name('declarator'), src)
            if not name or name in seen:     # #ifdef / #else twins: the first one
                continue
            seen.add(name)
            f = self.Func(mod, name, n, src)
            f.static = 'static' in _storage(n, src)
            f.file = rel
            f.fields = set()
            self.params(n.child_by_field_name('declarator'), f)
            self.walk(n.child_by_field_name('body'), f, False)
            info['funcs'].append(f)
            if name == 'main' and not f.static:
                info['has_main'] = info['entry_like'] = True
        for f in info['funcs']:
            f.data |= self.data_of(f)
        top.lines = sum(d.end_point[0] - d.start_point[0] + 1 for d in self.decls)
        top.body = '\n'.join(_text(d, src) for d in self.decls)
        top.hash = _hash(top.body)
        top.file = rel
        if info['has_main']:                 # one entry blob: the file-scope code with main()
            top.direct.append((f'{mod}.main', False))
        info['top'] = top

    # ------------------------------------------------------------ file scope

    def collect(self, n):
        src = self.src
        for c in n.children:
            t = c.type
            if t == 'function_definition':
                self.fdefs.append(c)
            elif t == 'declaration':
                static = 'static' in _storage(c, src)
                var = False
                for d in c.children_by_field_name('declarator'):
                    if d.type != 'init_declarator' and _is_proto(d):
                        continue
                    name = _declname(d, src)
                    if name:
                        self.vars.setdefault(name, static)
                        var = True
                if var:
                    self.decls.append(c)
                self.enumerators(c)
            elif t == 'type_definition':
                self.enumerators(c)
            elif t == 'preproc_function_def':
                nm = c.child_by_field_name('name')
                if nm is not None:
                    self.macros.add(_text(nm, src))
            elif t == 'preproc_include':
                self.include(c)
            elif t in NESTING:
                self.collect(c)

    def enumerators(self, n):
        stack = [n]
        while stack:
            c = stack.pop()
            if c.type == 'enumerator':
                nm = c.child_by_field_name('name')
                if nm is not None:
                    self.enums.add(_text(nm, self.src))
            elif c.type not in ('compound_statement', 'initializer_list'):
                stack.extend(c.named_children)

    def include(self, n):
        """#include "x.h" depends on the header module and the .c it pairs with; <x.h> is a system header.
        Both are noted in info['includes'] (link() checks which are missing from the snapshot)."""
        p = n.child_by_field_name('path')
        if p is not None and p.type == 'system_lib_string':      # <x.h>: only ever a library header
            self.info['includes'].append(_text(p, self.src)[1:-1])
            return
        spec = _strval(p, self.src)
        if spec is None:
            return
        self.info['includes'].append(spec)
        for cand in include_paths(self.rel, spec):
            stem, ext = posixpath.splitext(cand)
            for dep in (modname(cand), modname(stem + '.c')) if ext == '.h' else (modname(cand),):
                if dep != self.mod and dep not in self.info['deps']:
                    self.info['deps'].append(dep)

    # ------------------------------------------------------------ names

    def bind(self, name, f):
        """A called / passed name -> its import target (see the module doc). Locals never are."""
        if name in f.locals or name in self.info['imports']:
            return
        macro = name in self.macros or CAPS.fullmatch(name)
        self.info['imports'][name] = 'ext:' + name if macro or external(name) else f'{self.mod}.{name}'

    def params(self, d, f):
        while d is not None and d.type != 'function_declarator':
            d = d.child_by_field_name('declarator')
        plist = d.child_by_field_name('parameters') if d is not None else None
        for p in plist.named_children if plist is not None else ():
            if p.type == 'parameter_declaration':
                name = _declname(p.child_by_field_name('declarator'), self.src)
                if name:
                    f.locals.add(name)

    def deferred(self, v, f, err):
        """`fn` / `&fn` as a value: a deferred call (resolved strictly, so variables fall away)."""
        if v is not None and v.type == 'pointer_expression' and _text(v.child_by_field_name('operator'), self.src) == '&':
            v = v.child_by_field_name('argument')
        if v is not None and v.type == 'identifier':
            name = _text(v, self.src)
            if name not in f.locals and name not in EXTERNAL_DATA:
                self.bind(name, f)
                f.calls.append((name, err, 'deferred'))

    def data_of(self, f):
        """global:<name> for file-scope / extern variables the function reads or writes (static ones are
        global:<module>.<name>); a field access on one is global:<name>.<field>."""
        out = set()
        for name in f.names - f.locals:
            s = self.global_sym(name)
            if s:
                out.add(s)
        for name, field in f.fields:
            if name not in f.locals:
                s = self.global_sym(name)
                if s:
                    out.add(f'{s}.{field}')
        return out

    def global_sym(self, name):
        if name in EXTERNAL_DATA:
            return None
        if name in self.vars:
            return f'global:{self.mod}.{name}' if self.vars[name] else f'global:{name}'
        # not declared here: an extern from a header, unless it looks like a function, macro, constant or type (x_t)
        if name in self.fnames or name in self.called or name in self.macros or name in self.enums or \
                external(name) or name[:1].isupper() or CAPS.fullmatch(name) or name.endswith('_t'):
            return None
        return f'global:{name}'

    # ------------------------------------------------------------ the walk

    def walk(self, n, f, err):
        src, t = self.src, n.type
        if t in SKIP_TYPES:
            return
        if t in BRANCH_TYPES:
            f.branches += 1
        elif t == 'binary_expression':
            op = n.child_by_field_name('operator')
            if op is not None and op.type in ('&&', '||'):
                f.branches += 1
        if t == 'if_statement':
            for i, c in enumerate(n.children):
                fld = n.field_name_for_child(i)
                if fld == 'consequence':
                    self.walk_branch(c, f, err)
                elif fld == 'alternative':                 # else_clause: else <statement>
                    for d in c.named_children:
                        self.walk_branch(d, f, err)
                else:
                    self.walk(c, f, err)
            return
        if t == 'compound_statement':
            self.walk_block(n, f, err, set())
            return
        if t in ('preproc_if', 'preproc_elif', 'preproc_ifdef', 'preproc_elifdef'):
            for i, c in enumerate(n.children):
                if n.field_name_for_child(i) not in ('condition', 'name'):
                    self.walk(c, f, err)
            return
        if t == 'call_expression':
            self.call(n, f, err)
            return
        if t == 'identifier':
            f.names.add(_text(n, src))
            return
        if t == 'field_expression':
            arg = n.child_by_field_name('argument')
            fld = n.child_by_field_name('field')
            if arg is not None and arg.type == 'identifier' and fld is not None:
                if hasattr(f, 'fields'):
                    f.fields.add((_text(arg, src), _text(fld, src)))
                return
            if arg is not None:
                self.walk(arg, f, err)
            return
        if t in ('declaration', 'parameter_declaration'):
            for d in n.children_by_field_name('declarator'):
                name = _declname(d, src)
                if name and f.name != '<module>':
                    f.locals.add(name)
                if d.type == 'init_declarator':
                    v = d.child_by_field_name('value')
                    self.deferred(v, f, err)
                    if v is not None:
                        self.walk(v, f, err)
            return
        if t == 'assignment_expression':
            self.deferred(n.child_by_field_name('right'), f, err)
        elif t == 'initializer_list':
            for c in n.named_children:
                self.deferred(c, f, err)
        elif t == 'initializer_pair':
            self.deferred(n.child_by_field_name('value'), f, err)
        elif t == 'string_literal':
            s = _strval(n, src)
            if s is not None:
                for q in SQL.findall(s) if SQL_STMT.match(s) else ():
                    f.data.add('table:' + q.lower())
                if PATH.fullmatch(s) and ('.' in s or '/' in s):
                    f.strings.append(s)
            return
        for c in n.children:
            self.walk(c, f, err)

    def walk_block(self, block, f, err, errs):
        """Statements of a { } block; after a `fail:` / `err:` label the rest is the error path."""
        after = False
        for i, c in enumerate(block.children):
            if c.type == 'labeled_statement':
                lb = c.child_by_field_name('label')
                after = after or (lb is not None and bool(ERR_LABEL.fullmatch(_text(lb, self.src))))
            self.walk(c, f, err or after or i in errs)

    def walk_branch(self, b, f, err):
        if b.type == 'compound_statement':
            self.walk_block(b, f, err, self.error_children(b, f))
        else:
            e = self.is_error_exit(b)
            if e and b.type != 'expression_statement':     # exit() / abort() count in call()
                f.errors += 1
            self.walk(b, f, err or e)

    def error_children(self, block, f):
        """When a branch ends in an error exit, its statements after the last one that can still leave
        normally are on the error path (only the branch's last statement decides)."""
        kids = block.children
        idx = [i for i, c in enumerate(kids) if c.is_named and c.type != 'comment']
        if not idx or not self.is_error_exit(kids[idx[-1]]):
            return set()
        if kids[idx[-1]].type != 'expression_statement':
            f.errors += 1
        start = max((i + 1 for i in idx[:-1] if self.has_normal_exit(kids[i])), default=0)
        return {i for i in idx if i >= start}

    def is_error_exit(self, n):
        src = self.src
        if n.type == 'return_statement':
            v = n.named_children[0] if n.named_children else None
            while v is not None and v.type == 'parenthesized_expression' and v.named_children:
                v = v.named_children[0]
            if v is None:
                return False
            if v.type in ('null', 'false'):
                return True
            if v.type == 'number_literal':
                return _text(v, src).startswith('-')
            if v.type == 'unary_expression':
                op = v.child_by_field_name('operator')
                return op is not None and op.type == '-'
            return v.type == 'identifier' and _text(v, src) == 'NULL'
        if n.type == 'goto_statement':
            lb = n.child_by_field_name('label')
            return lb is not None and bool(ERR_LABEL.fullmatch(_text(lb, src)))
        if n.type == 'expression_statement' and n.named_children and n.named_children[0].type == 'call_expression':
            fn = n.named_children[0].child_by_field_name('function')
            return fn is not None and fn.type == 'identifier' and _text(fn, src) in EXIT_CALLS
        return False

    def has_normal_exit(self, n):
        if n.type == 'return_statement':
            return not self.is_error_exit(n)
        return any(self.has_normal_exit(c) for c in n.named_children)

    def call(self, n, f, err):
        src = self.src
        fn = n.child_by_field_name('function')
        args = n.child_by_field_name('arguments')
        args = [a for a in args.named_children if a.type != 'comment'] if args is not None else []
        callee = _norm(_text(fn, src))
        last = callee.rsplit('.', 1)[-1]
        if fn.type == 'parenthesized_expression' and fn.named_children and fn.named_children[0].type == 'identifier':
            for a in args:                      # (time_t)(x): a cast the grammar reads as a call
                self.walk(a, f, err)
            return
        if fn.type == 'identifier':
            self.called.add(callee)
            self.bind(callee, f)
        elif fn.type == 'parenthesized_expression' and re.fullmatch(r'[A-Za-z_]\w*', callee):
            f.names.add(callee)                 # (*fp)(x): reads fp
        else:
            self.walk(fn, f, err)
        if fn.type == 'identifier' and callee in EXIT_CALLS:
            f.errors += 1
        f.calls.append((callee, err or bool(ERROR_CALLEE.fullmatch(last)), 'call'))
        for a in args:
            self.deferred(a, f, err)
        if fn.type == 'identifier' and callee in CALLBACK_ARGS and len(args) > CALLBACK_ARGS[callee] and \
                args[CALLBACK_ARGS[callee]].type == 'identifier':
            self.called.add(_text(args[CALLBACK_ARGS[callee]], src))     # qsort(..., cmp): cmp is a function
        if fn.type == 'identifier':
            if callee in FILE_CALLS:
                i = FILE_CALLS[callee] or 0
                v = _strval(args[i], src) if len(args) > i else None
                if v:
                    f.data.add('file:' + v)
            elif callee in ENV_CALLS and args:
                v = _strval(args[0], src)
                if v:
                    f.data.add('env:' + v)
        for a in args:
            self.walk(a, f, err)
