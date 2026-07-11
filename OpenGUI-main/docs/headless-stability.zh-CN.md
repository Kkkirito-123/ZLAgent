# OpenGUI 无页面稳定运行设计

本文记录当前 Android 客户端的稳定性目标、ASR 取舍和验证门槛。目标不是展示更多界面，而是给 OpenGUI/ZLAgent 执行链路提供最少干扰、可恢复、可验证的运行环境。

## 目标状态

- 启动 OpenGUI 后不进入 Home 页面，只保留顶部灵动岛和后台 standby 连接。
- 灵动岛日常处于待机；点击后直接语音输入任务。
- 微信、IM、API 等远程下发仍走 standby socket，保持原逻辑。
- 本机语音入口把语音转成文本后直接进入服务端任务创建/执行，再由本机连接 execution socket，避免多余的 standby dispatch 回环。
- 任务执行期间不弹旧的大执行面板；只更新灵动岛状态。
- 任务结束、取消、异常恢复后回到待机灵动岛。
- 主 APK 不引入高风险 native ASR SDK 或大模型依赖。

## 当前链路

```text
Launcher
  -> PromotorApplication
  -> HeadlessAgentRuntime
  -> StandbyForegroundService
  -> Dynamic Island

Remote command
  -> server /api/remote-control/tasks/do or tasks/run
  -> standby:dispatch
  -> HeadlessAgentRuntime.handleDispatch
  -> execution websocket
  -> ActionExecutor

Local voice command
  -> Dynamic Island tap
  -> VoiceCommandActivity
  -> Android SpeechRecognizer
  -> server /api/remote-control/tasks/do { dispatch: false }
  -> HeadlessAgentRuntime.handleDispatch from HTTP response
  -> execution websocket
  -> ActionExecutor
```

## ASR 方案

当前主线使用 Android `SpeechRecognizer`：

- Android 12+ 优先 `createOnDeviceSpeechRecognizer`，系统支持本地识别时不走云。
- 否则回退到系统默认 speech recognition service。
- `EXTRA_PREFER_OFFLINE` 作为偏好，而不是硬依赖；设备不支持离线时不能阻断任务入口。
- 使用透明 Activity 承载运行时麦克风权限，避免 Application context 弹窗导致 `BadTokenException`。
- 如果设备没有可用系统 `SpeechRecognizer`，点击灵动岛直接切到文本命令兜底，不再启动可能只弹错误提示的外部语音界面。

暂不把 whisper.cpp、sherpa-onnx、FunASR 等本地 ASR 直接塞入主 APK：

- 会引入新的 native libraries、模型文件、ABI 适配和 16 KB page size 风险。
- 会增加冷启动、内存、CPU 和电量压力。
- Android 端主任务瓶颈通常是 VLM/执行链路稳定性，本地 ASR 只有在离线、隐私或系统识别不可用时才值得作为插件化能力。

推荐后续形态：

- 主 APK 保持系统 ASR。
- 可选离线 ASR 做独立插件包或动态模型包。
- 插件必须通过 16 KB page size、冷启动、内存峰值、连续 50 次识别、后台恢复和弱网回退验证后再启用。

参考实现与资料：

- Android SpeechRecognizer: https://developer.android.com/reference/android/speech/SpeechRecognizer
- Android 16 KB page size: https://developer.android.com/guide/practices/page-sizes
- sherpa-onnx Android examples: https://github.com/k2-fsa/sherpa-onnx
- whisper.cpp Android example: https://github.com/ggerganov/whisper.cpp
- FunASR: https://github.com/modelscope/FunASR

## 稳定性约束

- 不恢复旧 A11y tree 推理路径；执行仍以 GUI/vision-first 为主。
- 不引入商业 SDK、私有 endpoint、硬编码凭据或云日志。
- 不用关键词一刀切拦截任务；语音文本完整交给 ZLAgent/OpenGUI 服务端处理。
- 无页面模式下所有旧窗口只能作为执行内部组件存在，不允许主动展示 Home/Execute 大页面。
- 任何后台启动 foreground service 的失败都必须日志化，不能让进程崩溃。
- `dispatch=false` 仅用于本机已经拿到 execution id 且会直接连接 execution socket 的场景；远程/IM 默认仍 dispatch。

## 验证清单

每次改动至少验证：

- `cd client && ./gradlew assembleDebug`
- `cd server && pnpm build`
- 相关 remote-control 测试
- APK native library 列表不包含 `libspeechengine.so`、`libttcrypto.so`、`libttboringssl.so`、`libsscronet.so`、`libaudioeffect.so`
- APK 内剩余 native library 的 `PT_LOAD.p_align` 满足 16 KB
- 真机启动后：
  - OpenGUI 进程存活
  - `StandbyForegroundService` 存活
  - 顶部 `APPLICATION_OVERLAY` 存在
  - 前台 Activity 不是 Home 页面
  - logcat 没有 `FATAL EXCEPTION`、`BadTokenException`、`UnsatisfiedLinkError`、16 KB page size 相关错误
