# mac-computer-use

让 AI Agent 直接操作 macOS：看屏幕、点按钮、填输入框、跑多步流程。
**默认不抢鼠标**——走系统辅助功能树（AX），你在前台干活，它在后台动手；实在没有控件的界面才退回坐标点击。

三个工具，一个比一个高一档：

| 工具 | 干什么 | 抢鼠标 | 适合 |
| --- | --- | --- | --- |
| `cu` | 截屏 + 坐标点击/打字/按键 | 会 | 最后手段、canvas/游戏/拖拽 |
| `cu-ax` | 读控件树、按控件、改输入框 | 不会 | 日常操作（按钮、菜单、表单） |
| `cu-act` | 带验证地执行，失败自动重试 | 不会 | 连续多步、无人值守 |

## 安装

需要 macOS + 命令行工具（`xcode-select --install`，为了 `clang`）。

```bash
git clone https://github.com/<you>/mac-computer-use.git
cd mac-computer-use
make            # 编译 cu 和 cu-ax
./cu state      # 自检：权限 / 锁屏 / 屏幕倍率
```

然后授权一次：**系统设置 → 隐私与安全性 → 辅助功能**，把运行命令的 App（终端、iTerm、或你自己的宿主程序）勾上。
只截屏不需要这个权限，但点击/打字/读控件都需要。

想全局可用：`make install PREFIX=$HOME/.local`，并把 `$HOME/.local/bin` 加进 `PATH`。

## 用法

### cu —— 截屏与坐标操作

```bash
./cu state                  # {"locked":0,"post_events":1,"accessibility":1,"scale":2.00}
./cu shot /tmp/s.png        # 返回 JSON：pixel / point / scale
./cu click 800 450          # 坐标是「点」，不是像素：pixel ÷ scale
./cu type "你好 hello"
./cu key return             # return/tab/esc/up/down/cmd+a ...
```

### cu-ax —— 按控件，不动指针

```bash
./cu-ax apps                                # 列出可操作的 App 和 pid
./cu-ax tree --app 文本编辑 --depth 8        # 只读：窗口里有哪些控件
./cu-ax press --app 文本编辑 --title 保存
./cu-ax press --app 文本编辑 --at 725 226    # 按坐标找最近的控件再按（仍不动指针）
./cu-ax set  --app 备忘 --role AXTextArea --value "直接写进去"
./cu-ax enable --app Edge                   # Chromium 系先开这个，才看得到网页控件
```

`--app` 认中文名、英文名、bundle id；也可以 `--pid 1234`。

### cu-act —— 带验证的执行

```bash
# 单步：按完等「已保存」出现，最多等 5 秒，失败重试 2 次
./cu-act.py "app=文本编辑" --act press --title 保存 --expect 已保存

# 写值：读回来比对，最硬的验证
./cu-act.py "app=文本编辑" --act set --role AXTextArea --value "hello" --expect hello
```

多步流程用一个 JSON 文件：

```json
[
  {"act": "set", "role": "AXTextArea", "value": "这一段是 cu-act 自动写进去并核对的", "expect": "自动写进去"},
  {"act": "press", "title": "关于文本编辑", "expect": "版本", "timeout": 5}
]
```

```bash
./cu-act.py "app=文本编辑" --flow examples/demo.steps.json
```

每步输出一行 JSON；任何一步没成，整体退出码 1 并指出断在第几步，不会假装成功。

## 它怎么判断「成了」

1. **set**：读回控件值，和你要的字符串逐字比。
2. **press / click**：给了 `--expect` 就轮询等这段文字出现；没给就比较动作前后的控件树指纹，变了才算过。
3. 没成 → 等 0.4 秒重试，默认 2 次；还不行就报失败，带前后指纹。

坐标不计入指纹（动画会让坐标抖），只看角色、标题、值。

## 边界

- 只支持 macOS；Windows 没做。
- 锁屏时**拒绝**执行（会用 `cu state` 自查）。这条是故意的：锁屏时合成键盘事件可能打进密码框。
- 网页里拖拽、canvas、游戏、自绘界面没有控件树，只能退回 `cu` 的坐标点击，会占用鼠标。
- Chromium 系（Chrome/Edge）默认不暴露网页内容，要先 `cu-ax enable`。
- 界面一改版，硬编码的控件名就可能失效——用 `--expect` 写清「成功后应该看到什么」，比写死控件名稳。

## 安全

- 不联网，没有遥测，不传任何屏幕内容出去。
- 所有动作都在你给的 App 范围内；不主动切前台、不点系统授权弹窗。
- 建议别在跑流程时手动操作同一台机器——人和 Agent 抢同一个界面，验证会误判。

## 实测（2026-10-30，macOS 26.5 / M 系列）

| 项 | 结果 |
| --- | --- |
| 后台按菜单项「关于文本编辑」 | 0.088 秒，鼠标未动 |
| 按坐标找控件关闭窗口 | 成功（AXPress） |
| 往后台文档写入中文并读回核对 | 成功，前台未切换 |
| 控件命名率 | 322 个可点控件中 66 个无标题（约 20%），这部分只能靠坐标 |

## License

MIT

---

## 观察层与恢复层（2026-10-30）

`--act observe` 只吐一小段结构化状态，不再整棵树：

```json
{"window":["zz"],"modal":[],"focused":null,
 "controls":[{"n":"排序方式","r":"AXButton","p":[743,52]}],"count":18,"diff":["+排序方式"]}
```

失败时不再直接放弃，先诊断再救：

| 诊断 | 触发条件 | 自救动作 |
| --- | --- | --- |
| `modal` | 有 AXSheet / AXDialog | 按 `--dismiss 取消 关闭` 白名单点掉（默认不动弹窗） |
| `not_found` | 找不到目标控件 | 按字符集相似度找近名控件，按它的坐标按下去（仍不动鼠标） |
| `dead` | 进程已退出 | 立即报错，不空转 |
| `unknown` | 都不匹配 | 报错并指出断在第几步 |

```bash
./cu_act.py "app=文本编辑" --act press --title 保存 --expect 已保存 --dismiss 取消
```

### 验证状态（诚实版）

| 能力 | 状态 |
| --- | --- |
| observe 摘要 | ✅ 访达 / 文本编辑实测，输出 400 字节级 |
| 验证 + 重试 | ✅ 两步 flow 全绿，退出码 0 |
| 模糊匹配 | ✅ 单测：错字「排序方试」→ 命中「排序方式」；端到端仍待跑 |
| 弹窗识别 | ✅ 能识别并进入 modal 分支；点掉动作在样本 App 上会被打断 |
| 模糊后自动按下去 | ⚠️ 代码有，真机未跑通，先当实验特性 |

## 已知问题

- `cu-ax press` 在部分弹窗按钮上返回 -25204（AX 拒绝了这次 Perform），会退化成坐标按。
- 文本编辑这类「启动就弹恢复/标尺面板」的 App 不适合当测试靶子，会污染验证。
