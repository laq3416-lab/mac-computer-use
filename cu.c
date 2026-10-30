// cu —— 最小可用的 computer use 命令行工具（macOS，纯 C，无第三方依赖）
//
// 子命令：shot / move / click / scroll / type / key / perm
// 坐标一律用「屏幕点（point）」，不是截图像素。Retina 屏上截图像素 = 点 × scale。
//
// 编译：
//   clang -O2 -fmodules-cache-path=build/mc -o cu cu.c \
//     -framework ApplicationServices -framework CoreFoundation
//
// 权限：系统设置 → 隐私与安全性 → 辅助功能，勾上宿主 App（DeepSeek Harness）。

#include <ApplicationServices/ApplicationServices.h>
#include <CoreFoundation/CoreFoundation.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static void die(const char *msg) {
    fprintf(stderr, "%s\n", msg);
    exit(1);
}

// ---------- 屏幕 ----------

static void display_info(double *w, double *h, double *scale) {
    CGDirectDisplayID d = CGMainDisplayID();
    CGRect b = CGDisplayBounds(d);
    *w = b.size.width;
    *h = b.size.height;
    *scale = 1.0;
    CGDisplayModeRef m = CGDisplayCopyDisplayMode(d);
    if (m) {
        size_t px = CGDisplayModeGetPixelWidth(m);
        size_t pw = CGDisplayModeGetWidth(m);
        if (pw > 0) *scale = (double)px / (double)pw;
        CGDisplayModeRelease(m);
    }
}

// ---------- 截屏 ----------

static void shot(const char *path) {
    system("/usr/bin/caffeinate -u -t 1");   // 先把显示器唤醒，否则全是黑的
    usleep(300000);
    const char *file = path ? path : "/tmp/cu-shot.png";
    char cmd[1024];
    // -x 静音；-o 去掉窗口阴影；-r 不写 Retina 元数据，尺寸才和像素严格对应
    snprintf(cmd, sizeof(cmd), "/usr/sbin/screencapture -x -o '%s'", file);
    int rc = system(cmd);
    if (rc != 0) die("screencapture failed");

    double w, h, scale;
    display_info(&w, &h, &scale);
    // 以文件真实像素为准（新系统上 CGDisplayPixelsWide 返回的是点数）
    size_t pxw = 0, pxh = 0;
    CFURLRef url = CFURLCreateFromFileSystemRepresentation(NULL, (const UInt8 *)file, strlen(file), false);
    CGImageSourceRef src = CGImageSourceCreateWithURL(url, NULL);
    if (src) {
        CFDictionaryRef props = CGImageSourceCopyPropertiesAtIndex(src, 0, NULL);
        if (props) {
            CFNumberRef nw = CFDictionaryGetValue(props, kCGImagePropertyPixelWidth);
            CFNumberRef nh = CFDictionaryGetValue(props, kCGImagePropertyPixelHeight);
            if (nw) CFNumberGetValue(nw, kCFNumberLongType, &pxw);
            if (nh) CFNumberGetValue(nh, kCFNumberLongType, &pxh);
            CFRelease(props);
        }
        CFRelease(src);
    }
    CFRelease(url);
    if (w > 0 && pxw > 0) scale = (double)pxw / w;
    printf("{\"path\":\"%s\",\"pixel\":[%zu,%zu],\"point\":[%.0f,%.0f],\"scale\":%.2f}\n",
           file, pxw, pxh, w, h, scale);
}

// ---------- 鼠标 ----------

static void post_move(CGPoint pt) {
    CGEventRef e = CGEventCreateMouseEvent(NULL, kCGEventMouseMoved, pt, kCGMouseButtonLeft);
    CGEventPost(kCGHIDEventTap, e);
    CFRelease(e);
    usleep(30000);
}

static void click(double x, double y, int right, int times) {
    CGPoint pt = CGPointMake(x, y);
    post_move(pt);
    CGEventType down = right ? kCGEventRightMouseDown : kCGEventLeftMouseDown;
    CGEventType up   = right ? kCGEventRightMouseUp   : kCGEventLeftMouseUp;
    CGMouseButton b  = right ? kCGMouseButtonRight : kCGMouseButtonLeft;
    for (int i = 0; i < (times > 1 ? times : 1); i++) {
        CGEventRef d = CGEventCreateMouseEvent(NULL, down, pt, b);
        CGEventSetIntegerValueField(d, kCGMouseEventClickState, i + 1);
        CGEventPost(kCGHIDEventTap, d);
        CFRelease(d);
        usleep(60000);
        CGEventRef u = CGEventCreateMouseEvent(NULL, up, pt, b);
        CGEventSetIntegerValueField(u, kCGMouseEventClickState, i + 1);
        CGEventPost(kCGHIDEventTap, u);
        CFRelease(u);
        usleep(80000);
    }
}

static void scroll_at(double x, double y, int dy) {
    post_move(CGPointMake(x, y));
    CGEventRef e = CGEventCreateScrollWheelEvent(NULL, kCGScrollEventUnitPixel, 1, dy);
    CGEventSetLocation(e, CGPointMake(x, y));
    CGEventPost(kCGHIDEventTap, e);
    CFRelease(e);
}

// ---------- 键盘 ----------

static int32_t code_for(const char *name) {
    struct { const char *n; int32_t c; } map[] = {
        {"return", 36}, {"enter", 36}, {"tab", 48}, {"space", 49}, {"esc", 53},
        {"escape", 53}, {"delete", 51}, {"backspace", 51}, {"up", 126}, {"down", 125},
        {"left", 123}, {"right", 124}, {"home", 115}, {"end", 119},
        {"pageup", 116}, {"pagedown", 121}, {"a", 0}, {"c", 8}, {"v", 9},
        {"s", 1}, {"w", 13}, {"q", 12}, {"z", 6}, {"f", 3}, {"r", 15},
    };
    for (size_t i = 0; i < sizeof(map) / sizeof(map[0]); i++)
        if (strcmp(name, map[i].n) == 0) return map[i].c;
    return -1;
}

static void type_text(const char *utf8) {
    CFStringRef s = CFStringCreateWithCString(NULL, utf8, kCFStringEncodingUTF8);
    if (!s) die("bad utf8");
    CFIndex len = CFStringGetLength(s);
    UniChar *buf = malloc(sizeof(UniChar) * (len + 1));
    CFStringGetCharacters(s, CFRangeMake(0, len), buf);
    CFRelease(s);

    // 一次塞太多会丢字，按 16 个 UTF-16 单元切
    for (CFIndex i = 0; i < len; i += 16) {
        CFIndex n = (len - i < 16) ? (len - i) : 16;
        CGEventRef down = CGEventCreateKeyboardEvent(NULL, 0, true);
        CGEventKeyboardSetUnicodeString(down, n, buf + i);
        CGEventPost(kCGHIDEventTap, down);
        CFRelease(down);
        usleep(15000);
        CGEventRef up = CGEventCreateKeyboardEvent(NULL, 0, false);
        CGEventKeyboardSetUnicodeString(up, n, buf + i);
        CGEventPost(kCGHIDEventTap, up);
        CFRelease(up);
        usleep(15000);
    }
    free(buf);
}

static void key_press(const char *name, int cmd) {
    int32_t code = code_for(name);
    if (code < 0) die("unknown key");
    CGEventFlags flags = cmd ? kCGEventFlagMaskCommand : 0;
    CGEventRef d = CGEventCreateKeyboardEvent(NULL, (CGKeyCode)code, true);
    CGEventSetFlags(d, flags);
    CGEventPost(kCGHIDEventTap, d);
    CFRelease(d);
    usleep(40000);
    CGEventRef u = CGEventCreateKeyboardEvent(NULL, (CGKeyCode)code, false);
    CGEventSetFlags(u, flags);
    CGEventPost(kCGHIDEventTap, u);
    CFRelease(u);
}

// ---------- 锁屏 / 权限 ----------

static int screen_locked(void) {
    int locked = 0;
    CFDictionaryRef d = CGSessionCopyCurrentDictionary();
    if (d) {
        CFBooleanRef b = CFDictionaryGetValue(d, CFSTR("CGSSessionScreenIsLocked"));
        if (b && CFBooleanGetValue(b)) locked = 1;
        CFRelease(d);
    }
    return locked;
}

// 输入类命令的前置检查：锁屏时一律拒动，免得把字打进密码框
static void require_unlocked(void) {
    if (screen_locked()) {
        fprintf(stderr, "screen is locked; refusing to send input\n");
        exit(2);
    }
    if (!CGPreflightPostEventAccess()) {
        fprintf(stderr, "no accessibility permission; grant it to the host app first\n");
        exit(3);
    }
}

static void perm(int request) {
    int post = CGPreflightPostEventAccess();
    int ax = AXIsProcessTrusted();
    if (request && !post) {
        CGRequestPostEventAccess();  // 弹系统授权框
    }
    printf("{\"post_events\":%d,\"accessibility\":%d}\n", post, ax);
}

// ---------- 入口 ----------

int main(int argc, char **argv) {
    if (argc < 2) die("usage: cu shot [path] | move X Y | click X Y [--right] [--double] | "
                      "scroll X Y DY | type TEXT | key NAME [--cmd] | perm [--request]");
    const char *cmd = argv[1];

    if (!strcmp(cmd, "state")) {
        double w, h, sc;
        display_info(&w, &h, &sc);
        printf("{\"locked\":%d,\"post_events\":%d,\"accessibility\":%d,\"point\":[%.0f,%.0f],\"scale\":%.2f}\n",
               screen_locked(), CGPreflightPostEventAccess(), AXIsProcessTrusted(), w, h, sc);
        return 0;
    }
    if (!strcmp(cmd, "shot")) {
        shot(argc > 2 ? argv[2] : NULL);
    } else if (!strcmp(cmd, "move")) {
        if (argc < 4) die("move X Y");
        require_unlocked();
        post_move(CGPointMake(atof(argv[2]), atof(argv[3])));
    } else if (!strcmp(cmd, "click")) {
        if (argc < 4) die("click X Y");
        int right = 0, times = 1;
        for (int i = 4; i < argc; i++) {
            if (!strcmp(argv[i], "--right")) right = 1;
            if (!strcmp(argv[i], "--double")) times = 2;
        }
        require_unlocked();
        click(atof(argv[2]), atof(argv[3]), right, times);
    } else if (!strcmp(cmd, "scroll")) {
        if (argc < 5) die("scroll X Y DY");
        require_unlocked();
        scroll_at(atof(argv[2]), atof(argv[3]), atoi(argv[4]));
    } else if (!strcmp(cmd, "type")) {
        if (argc < 3) die("type TEXT");
        char buf[4096] = {0};
        for (int i = 2; i < argc; i++) {
            if (i > 2) strncat(buf, " ", sizeof(buf) - strlen(buf) - 2);
            strncat(buf, argv[i], sizeof(buf) - strlen(buf) - 2);
        }
        require_unlocked();
        type_text(buf);
    } else if (!strcmp(cmd, "key")) {
        if (argc < 3) die("key NAME");
        int cmdflag = 0;
        for (int i = 3; i < argc; i++) if (!strcmp(argv[i], "--cmd")) cmdflag = 1;
        require_unlocked();
        key_press(argv[2], cmdflag);
    } else if (!strcmp(cmd, "perm")) {
        perm(argc > 2 && !strcmp(argv[2], "--request"));
    } else {
        die("unknown command");
    }
    return 0;
}
