// cu-ax —— 不抢鼠标的 computer use（macOS Accessibility API）
//
// 思路和 cu 不一样：cu 是「挪鼠标、真点」，这个只跟控件对话：
//   找控件（按名字/角色） → 按它（AXPress） → 或者直接改它的值（AXValue）
// 全程不移动指针、不抢你正在用的鼠标，只在你指定的那个 App 里生效。
//
// 编译：
//   clang -O2 -fmodules-cache-path=build/mc -o cu-ax cu_ax.c \
//     -framework ApplicationServices -framework AppKit -framework CoreFoundation
//
// 依赖：系统设置 → 隐私与安全性 → 辅助功能 → 勾上宿主 App

#import <ApplicationServices/ApplicationServices.h>
#import <AppKit/AppKit.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int g_max = 400;
static int g_count = 0;

static void die(const char *m) { fprintf(stderr, "%s\n", m); exit(1); }

static int ax_ok(void) { return AXIsProcessTrusted(); }

// ---------- App 解析 ----------

typedef struct { pid_t pid; char name[256]; char bundle[256]; char exe[128]; } AppInfo;

static int list_apps(AppInfo *out, int cap) {
    int n = 0;
    NSArray *apps = [[NSWorkspace sharedWorkspace] runningApplications];
    for (NSRunningApplication *a in apps) {
        if (a.activationPolicy != NSApplicationActivationPolicyRegular) continue;
        if (n >= cap) break;
        out[n].pid = a.processIdentifier;
        const char *nm = a.localizedName.UTF8String;
        const char *bid = a.bundleIdentifier.UTF8String;
        const char *exe = a.executableURL.lastPathComponent.UTF8String;
        snprintf(out[n].name, sizeof(out[n].name), "%s", nm ? nm : "?");
        snprintf(out[n].bundle, sizeof(out[n].bundle), "%s", bid ? bid : "");
        snprintf(out[n].exe, sizeof(out[n].exe), "%s", exe ? exe : "");
        n++;
    }
    return n;
}

static pid_t frontmost_pid(void) {
    NSRunningApplication *a = [[NSWorkspace sharedWorkspace] frontmostApplication];
    return a ? a.processIdentifier : 0;
}

static pid_t resolve_pid(const char *want) {
    if (!strcasecmp(want, "front")) return frontmost_pid();
    if (want[0] >= '0' && want[0] <= '9') return (pid_t)atoi(want);
    AppInfo apps[256];
    int n = list_apps(apps, 256);
    for (int i = 0; i < n; i++) {
        if (strcasestr(apps[i].name, want) || strcasestr(apps[i].bundle, want) ||
            strcasestr(apps[i].exe, want))
            return apps[i].pid;
    }
    return 0;
}

// ---------- 属性小工具 ----------

static CFTypeRef attr(AXUIElementRef e, CFStringRef name) {
    CFTypeRef v = NULL;
    if (AXUIElementCopyAttributeValue(e, name, &v) != kAXErrorSuccess) return NULL;
    return v;
}

static char *str_attr(AXUIElementRef e, CFStringRef name) {
    CFTypeRef v = attr(e, name);
    if (!v) return NULL;
    if (CFGetTypeID(v) != CFStringGetTypeID()) { CFRelease(v); return NULL; }
    CFIndex len = CFStringGetLength(v) + 1;
    char *buf = calloc(1, len * 4);
    CFStringGetCString(v, buf, len * 4, kCFStringEncodingUTF8);
    CFRelease(v);
    return buf;
}

static int point_attr(AXUIElementRef e, CFStringRef name, double *x, double *y) {
    CFTypeRef v = attr(e, name);
    if (!v) return 0;
    CGPoint p = CGPointZero;
    int ok = AXValueGetValue(v, kAXValueTypeCGPoint, &p);
    CFRelease(v);
    *x = p.x; *y = p.y;
    return ok;
}

static int size_attr(AXUIElementRef e, CFStringRef name, double *w, double *h) {
    CFTypeRef v = attr(e, name);
    if (!v) return 0;
    CGSize s = CGSizeZero;
    int ok = AXValueGetValue(v, kAXValueTypeCGSize, &s);
    CFRelease(v);
    *w = s.width; *h = s.height;
    return ok;
}

static int has_action(AXUIElementRef e, CFStringRef action) {
    CFArrayRef acts = NULL;
    if (AXUIElementCopyActionNames(e, &acts) != kAXErrorSuccess || !acts) return 0;
    int found = 0;
    for (CFIndex i = 0; i < CFArrayGetCount(acts); i++) {
        if (CFEqual(CFArrayGetValueAtIndex(acts, i), action)) { found = 1; break; }
    }
    CFRelease(acts);
    return found;
}

// ---------- 遍历 ----------

typedef void (*NodeFn)(AXUIElementRef e, int depth, void *ctx);

static void walk(AXUIElementRef e, int depth, int maxdepth, NodeFn fn, void *ctx) {
    if (g_count >= g_max) return;
    fn(e, depth, ctx);
    g_count++;
    if (depth >= maxdepth) return;

    CFTypeRef kids = attr(e, kAXChildrenAttribute);
    if (!kids) return;
    if (CFGetTypeID(kids) != CFArrayGetTypeID()) { CFRelease(kids); return; }
    CFIndex n = CFArrayGetCount(kids);
    for (CFIndex i = 0; i < n; i++) {
        AXUIElementRef k = (AXUIElementRef)CFArrayGetValueAtIndex(kids, i);
        walk(k, depth + 1, maxdepth, fn, ctx);
    }
    CFRelease(kids);
}

// ---------- tree ----------

typedef struct { const char *filter; int shown; } TreeCtx;

static void print_node(AXUIElementRef e, int depth) {
    char *role = str_attr(e, kAXRoleAttribute);
    char *title = str_attr(e, kAXTitleAttribute);
    char *desc = str_attr(e, kAXDescriptionAttribute);
    char *val = str_attr(e, kAXValueAttribute);
    double x = 0, y = 0, w = 0, h = 0;
    point_attr(e, kAXPositionAttribute, &x, &y);
    size_attr(e, kAXSizeAttribute, &w, &h);

    // 跳过纯容器噪音：没有名字、没有位置、没有动作
    int pressable = has_action(e, kAXPressAction);
    int interesting = title || desc || pressable || (val && strlen(val) > 0) ||
                      (w > 0 && h > 0);
    if (interesting) {
        printf("{\"i\":%d,\"role\":\"%s\",\"title\":\"%s\",\"desc\":\"%s\",\"value\":\"%s\","
               "\"pos\":[%.0f,%.0f],\"size\":[%.0f,%.0f],\"press\":%d}\n",
               g_count, role ? role : "", title ? title : "", desc ? desc : "",
               val ? val : "", x, y, w, h, pressable);
        fflush(stdout);
    }
    free(role); free(title); free(desc); free(val);
}

static TreeCtx *g_tree_ctx;

static void tree_fn(AXUIElementRef e, int depth, void *ctx) {
    TreeCtx *c = (TreeCtx *)ctx;
    char *title = str_attr(e, kAXTitleAttribute);
    char *desc = str_attr(e, kAXDescriptionAttribute);
    char *role = str_attr(e, kAXRoleAttribute);
    int match = 1;
    if (c->filter) {
        match = (title && strcasestr(title, c->filter)) ||
                (desc && strcasestr(desc, c->filter)) ||
                (role && strcasestr(role, c->filter));
    }
    free(title); free(desc); free(role);
    if (match) print_node(e, depth);
}

// ---------- 找控件并动作 ----------

typedef struct {
    const char *title;     // 名字匹配（子串，忽略大小写）
    const char *role;      // 角色匹配
    int index;             // 第几个匹配（从 1 开始）
    int seen;
    int want_value;        // set 模式：找可写的
    double ax, ay;         // --at：按坐标找（±25 点内、可点击）
    int want_at;
    AXUIElementRef hit;
} FindCtx;

static int matches(AXUIElementRef e, FindCtx *c) {
    char *title = str_attr(e, kAXTitleAttribute);
    char *desc = str_attr(e, kAXDescriptionAttribute);
    char *role = str_attr(e, kAXRoleAttribute);
    int ok = 1;
    if (c->title && !((title && strcasestr(title, c->title)) ||
                      (desc && strcasestr(desc, c->title)))) ok = 0;
    if (c->role && !(role && strcasecmp(role, c->role) == 0)) ok = 0;
    free(title); free(desc); free(role);
    return ok;
}

static void find_fn(AXUIElementRef e, int depth, void *ctx) {
    FindCtx *c = (FindCtx *)ctx;
    if (c->hit) return;
    if (c->want_at) {
        double x = 0, y = 0, w = 0, h = 0;
        if (!point_attr(e, kAXPositionAttribute, &x, &y)) return;
        if (!size_attr(e, kAXSizeAttribute, &w, &h)) return;
        if (w < 4 || h < 4) return;                      // 太小的不算
        if (x - 25 > c->ax || c->ax > x + w + 25) return; // 只收包含该点的控件
        if (y - 25 > c->ay || c->ay > y + h + 25) return;
        if (!has_action(e, kAXPressAction)) return;
        c->seen++;
        if (c->seen >= c->index) c->hit = (AXUIElementRef)CFRetain(e);
        return;
    }
    if (!matches(e, c)) return;
    if (!c->want_value && !has_action(e, kAXPressAction)) return;
    if (c->want_value) {
        // 只要能写 AXValue 的文本控件
        CFTypeRef settable = attr(e, kAXRoleAttribute);
        (void)settable;
        Boolean can = false;
        AXUIElementIsAttributeSettable(e, kAXValueAttribute, &can);
        if (!can) return;
        if (settable) CFRelease(settable);
    }
    c->seen++;
    if (c->seen >= c->index) {
        c->hit = (AXUIElementRef)CFRetain(e);
    }
}

// ---------- 事件直投（不移动指针的备用方案） ----------

static void post_click_to_pid(pid_t pid, double x, double y) {
    CGPoint pt = CGPointMake(x, y);
    CGEventRef d = CGEventCreateMouseEvent(NULL, kCGEventLeftMouseDown, pt, kCGMouseButtonLeft);
    CGEventPostToPid(pid, d);
    CFRelease(d);
    usleep(60000);
    CGEventRef u = CGEventCreateMouseEvent(NULL, kCGEventLeftMouseUp, pt, kCGMouseButtonLeft);
    CGEventPostToPid(pid, u);
    CFRelease(u);
}

static void post_type_to_pid(pid_t pid, const char *utf8) {
    CFStringRef s = CFStringCreateWithCString(NULL, utf8, kCFStringEncodingUTF8);
    if (!s) return;
    CFIndex len = CFStringGetLength(s);
    UniChar *buf = malloc(sizeof(UniChar) * (len + 1));
    CFStringGetCharacters(s, CFRangeMake(0, len), buf);
    for (CFIndex i = 0; i < len; i += 16) {
        CFIndex n = (len - i < 16) ? (len - i) : 16;
        CGEventRef d = CGEventCreateKeyboardEvent(NULL, 0, true);
        CGEventKeyboardSetUnicodeString(d, n, buf + i);
        CGEventPostToPid(pid, d);
        CFRelease(d);
        usleep(12000);
    }
    free(buf);
    CFRelease(s);
}

// ---------- 入口 ----------

static void usage(void) {
    die("usage:\n"
        "  cu-ax apps                         列出可操作的 App\n"
        "  cu-ax tree   --pid P|--app A [--depth 6] [--max 400] [--filter 文字]\n"
        "  cu-ax press  --pid P|--app A --title 名字 [--role AXButton] [--index 1]\n"
        "  cu-ax press  --pid P|--app A --at X Y      按坐标找最近的控件再按（不动指针）\n"
        "  cu-ax set    --pid P|--app A --value 文字 [--index 1]\n"
        "  cu-ax focus  --pid P|--app A\n"
        "  cu-ax focused                      当前焦点控件（光标在哪）\n"
        "  cu-ax enable --pid P|--app A       让 Chromium 系暴露网页控件树\n"
        "  cu-ax evclick --pid P X Y          把点击直投给指定进程（不动指针）\n"
        "  cu-ax evtype  --pid P 文字\n");
}

int main(int argc, char **argv) {
    @autoreleasepool {
        if (argc < 2) usage();
        const char *cmd = argv[1];

        // 参数
        const char *app = NULL, *title = NULL, *role = NULL, *value = NULL, *filter = NULL;
        int depth = 6, index = 1;
        double atx = -1, aty = -1;
        pid_t pid = 0;
        for (int i = 2; i < argc; i++) {
            if (!strcmp(argv[i], "--app") && i + 1 < argc) app = argv[++i];
            else if (!strcmp(argv[i], "--pid") && i + 1 < argc) pid = (pid_t)atoi(argv[++i]);
            else if (!strcmp(argv[i], "--title") && i + 1 < argc) title = argv[++i];
            else if (!strcmp(argv[i], "--role") && i + 1 < argc) role = argv[++i];
            else if (!strcmp(argv[i], "--value") && i + 1 < argc) value = argv[++i];
            else if (!strcmp(argv[i], "--filter") && i + 1 < argc) filter = argv[++i];
            else if (!strcmp(argv[i], "--depth") && i + 1 < argc) depth = atoi(argv[++i]);
            else if (!strcmp(argv[i], "--max") && i + 1 < argc) g_max = atoi(argv[++i]);
            else if (!strcmp(argv[i], "--index") && i + 1 < argc) index = atoi(argv[++i]);
            else if (!strcmp(argv[i], "--at") && i + 2 < argc) atx = atof(argv[++i]), aty = atof(argv[++i]);
        }

        if (!strcmp(cmd, "apps")) {
            AppInfo apps[256];
            int n = list_apps(apps, 256);
            for (int i = 0; i < n; i++) printf("%6d  %s\n", apps[i].pid, apps[i].name);
            printf("ax_trusted=%d\n", ax_ok());
            return 0;
        }
        if (!strcmp(cmd, "evclick")) {
            if (argc < 5) usage();
            post_click_to_pid((pid_t)atoi(argv[2]), atof(argv[3]), atof(argv[4]));
            return 0;
        }
        if (!strcmp(cmd, "evtype")) {
            if (argc < 4) usage();
            post_type_to_pid((pid_t)atoi(argv[2]), argv[3]);
            return 0;
        }

        if (!strcmp(cmd, "focused")) {
            AXUIElementRef sys = AXUIElementCreateSystemWide();
            CFTypeRef fe = NULL;
            if (AXUIElementCopyAttributeValue(sys, kAXFocusedUIElementAttribute, &fe) == kAXErrorSuccess && fe) {
                AXUIElementRef el = (AXUIElementRef)fe;
                char *r = str_attr(el, kAXRoleAttribute);
                char *t = str_attr(el, kAXTitleAttribute);
                char *d = str_attr(el, kAXDescriptionAttribute);
                char *v = str_attr(el, kAXValueAttribute);
                printf("{\"role\":\"%s\",\"title\":\"%s\",\"desc\":\"%s\",\"value\":\"%s\"}\n",
                       r?r:"", t?t:"", d?d:"", v?v:"");
                free(r); free(t); free(d); free(v);
                CFRelease(fe);
            }
            CFRelease(sys);
            return 0;
        }

        if (!app && !pid) usage();
        if (!pid) pid = resolve_pid(app);
        if (!pid) die("app not found; try `cu-ax apps`");
        if (!ax_ok()) die("no accessibility permission; grant it to the host app");

        AXUIElementRef root = AXUIElementCreateApplication(pid);

        if (!strcmp(cmd, "tree")) {
            TreeCtx ctx = { filter, 0 };
            g_tree_ctx = &ctx;
            walk(root, 0, depth, tree_fn, &ctx);
            printf("{\"nodes\":%d}\n", g_count);
        } else if (!strcmp(cmd, "press") || !strcmp(cmd, "set")) {
            FindCtx ctx = {0};
            ctx.title = title;
            ctx.role = role;
            ctx.index = index;
            ctx.want_value = !strcmp(cmd, "set");
            ctx.want_at = (atx >= 0);
            ctx.ax = atx; ctx.ay = aty;
            walk(root, 0, 12, find_fn, &ctx);
            if (!ctx.hit) die("element not found");
            AXError err;
            if (ctx.want_value) {
                CFStringRef s = CFStringCreateWithCString(NULL, value ? value : "",
                                                         kCFStringEncodingUTF8);
                err = AXUIElementSetAttributeValue(ctx.hit, kAXValueAttribute, s);
                CFRelease(s);
            } else {
                err = AXUIElementPerformAction(ctx.hit, kAXPressAction);
            }
            printf("{\"action\":\"%s\",\"ok\":%d,\"error\":%d}\n", cmd,
                   err == kAXErrorSuccess, (int)err);
            CFRelease(ctx.hit);

        } else if (!strcmp(cmd, "enable")) {
            AXError e1 = AXUIElementSetAttributeValue(root, CFSTR("AXEnhancedUserInterface"), kCFBooleanTrue);
            AXError e2 = AXUIElementSetAttributeValue(root, CFSTR("AXManualAccessibility"), kCFBooleanTrue);
            printf("{\"enhanced\":%d,\"manual\":%d}\n", e1 == kAXErrorSuccess, e2 == kAXErrorSuccess);
        } else if (!strcmp(cmd, "focus")) {
            NSRunningApplication *ra = [NSRunningApplication runningApplicationWithProcessIdentifier:pid];
            BOOL ok = [ra activateWithOptions:NSApplicationActivateIgnoringOtherApps];
            printf("{\"focus\":%d}\n", ok);
        } else {
            usage();
        }
        CFRelease(root);
    }
    return 0;
}
