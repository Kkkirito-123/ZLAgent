---
name: phone-gui-assistant
description: >
  Use OpenGUI as ZLAgent's phone GUI executor. Trigger when the user asks to
  operate an Android phone, open or inspect a mobile app, read the current
  phone screen, check in-app notifications, or bind a phone executor.
version: 0.1.0
tags:
  - phone
  - gui
  - android
  - opengui
metadata:
  hermes:
    created_by: user
  zlagent:
    category: tool
    triggers:
      - 绑定手机
      - 手机执行器
      - gui执行器
      - GUI执行器
      - OpenGUI
      - open gui
      - 操作手机
      - 控制手机
      - 打开手机
      - 打开app
      - 打开 App
      - 看手机
      - 当前屏幕
      - 手机屏幕
      - 手机页面
      - 安卓
      - Android
      - app通知
      - App通知
      - 小红书
      - 抖音
      - 微信文件
---

# Phone GUI Assistant

Use the `open_gui` tool when the user's request needs a real Android phone
screen or a mobile app UI.

## Workflow

1. If the user asks to bind a phone executor:
   - Call `open_gui` with `action="devices"` first.
   - If exactly one device is online, ask the user to confirm binding it.
   - After confirmation, call `open_gui` with `action="bind"` and the device id.

2. If the user asks to operate or inspect the phone:
   - Call `open_gui` with `action="current_binding"` if you are unsure whether
     a device is already bound.
   - If no device is bound, tell the user to start the OpenGUI Android app and
     bind a device first.
   - If a device is bound, call `open_gui` with `action="do"` and a concise,
     explicit task description.

3. For ongoing executions:
   - Use `status` to inspect progress.
   - Use `pause`, `resume`, or `cancel` only when the user asks for that
     lifecycle action or when OpenGUI requires user feedback.

## Safety

Never instruct OpenGUI to complete payment, send a message, delete content,
authorize login, change security settings, or enter passwords / verification
codes without explicit user confirmation. For those moments, pause and ask
the user what to do next.

## Response Style

Keep the WeChat reply short. Tell the user which phone task was started and,
when available, include the `executionId` so the next message can check status
or resume the same execution.
