package com.coremate.opengui.feature.promotor.runtime

import android.annotation.SuppressLint
import android.app.Activity
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.provider.Settings
import android.widget.Toast
import com.coremate.opengui.accessibility.ActionExecutor
import com.coremate.opengui.common.TaskCenter
import com.coremate.opengui.common.log.LogManager
import com.coremate.opengui.common_jvm.event.AutomationEvent
import com.coremate.opengui.common_jvm.event.AutomationEventBus
import com.coremate.opengui.feature.promotor.PermissionManager
import com.coremate.opengui.feature.promotor.common.MessageController
import com.coremate.opengui.feature.promotor.common.feedback.ClickFeedbackView
import com.coremate.opengui.feature.promotor.ui.AIFloatWindowManager
import com.coremate.opengui.feature.promotor.ui.window.AccessibilityServiceWarningWindow
import com.coremate.opengui.feature.promotor.ui.window.CallUserWindow
import com.coremate.opengui.feature.promotor.ui.window.ExecuteTaskWindow
import com.coremate.opengui.feature.promotor.ui.window.GradientWindow
import com.coremate.opengui.feature.promotor.ui.window.SlideExpandWindow
import com.coremate.opengui.network.api.RetrofitClient
import com.coremate.opengui.network.api.ServerConstant
import com.coremate.opengui.network.api.task.RemoteDoTaskReq
import com.coremate.opengui.network.upload.ImageUploaderImpl
import com.coremate.opengui.network.websocket.StandbyForegroundService
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.collectLatest
import kotlinx.coroutines.launch

@SuppressLint("HardwareIds")
object HeadlessAgentRuntime {
    private const val TAG = "HeadlessRuntime"

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)
    private var appContext: Context? = null
    private var eventJob: Job? = null
    private var socketStateJob: Job? = null

    fun initialize(context: Context) {
        val applicationContext = context.applicationContext
        appContext = applicationContext
        AIFloatWindowManager.compactOnlyMode = true
        ensureWindows(applicationContext)
        ensureActionHandler(applicationContext)
        startStandbyService(applicationContext)
        startEventCollectors(applicationContext)
    }

    fun showStandbyIsland(from: String) {
        val context = appContext ?: return
        ensureWindows(context)
        AIFloatWindowManager.showStandbyIsland("Listening on standby", from)
    }

    fun submitTextCommand(context: Context, rawText: String): Boolean {
        initialize(context)
        val command = rawText.trim()
        if (command.isEmpty()) return true

        val appContext = context.applicationContext
        if (TaskCenter.executionId != null) {
            Toast.makeText(appContext, "A task is already running.", Toast.LENGTH_SHORT).show()
            AIFloatWindowManager.showStandbyIsland("Task running", "submit while running")
            return true
        }

        if (!PermissionManager.checkPermission(appContext, "Headless voice command")) {
            AIFloatWindowManager.showStandbyIsland("Grant permissions", "permission missing")
            requestMissingPermissions(context, appContext)
            return context !is Activity
        }

        val taskName = command.take(20).ifBlank { "Voice task" }
        AIFloatWindowManager.showStandbyIsland("Submitting task...", "voice command")
        TaskCenter.reset(appContext, "Headless voice command")
        TaskCenter.taskTitle = taskName
        TaskCenter.taskPrompt = command

        scope.launch(Dispatchers.IO) {
            val apiService = RetrofitClient.create(appContext)
            val deviceId = currentDeviceId(appContext)
            runCatching {
                apiService.doRemoteTask(
                    RemoteDoTaskReq(
                        description = command,
                        taskName = taskName,
                        deviceId = deviceId,
                        dispatch = false,
                    )
                )
            }.onSuccess { response ->
                val body = response.body()
                if (!response.isSuccessful || body?.success != true) {
                    LogManager.saveLog(
                        appContext,
                        TAG,
                        "Remote do task failed: code=${response.code()} body=$body",
                        -1
                    )
                    scope.launch {
                        Toast.makeText(appContext, "Task submission failed.", Toast.LENGTH_SHORT).show()
                        AIFloatWindowManager.showStandbyIsland("Standby", "submit failed")
                    }
                    return@onSuccess
                }
                handleDispatch(
                    appContext = appContext,
                    executionId = body.executionId,
                    taskId = body.taskId,
                    taskName = body.taskName,
                    from = "voice doTask response",
                )
            }.onFailure { error ->
                LogManager.saveLog(
                    appContext,
                    TAG,
                    "Remote do task request failed: ${error.message}",
                    -1
                )
                scope.launch {
                    Toast.makeText(appContext, "Task submission failed: ${error.message}", Toast.LENGTH_SHORT).show()
                    AIFloatWindowManager.showStandbyIsland("Standby", "submit exception")
                }
            }
        }
        return true
    }

    private fun requestMissingPermissions(requestContext: Context, appContext: Context) {
        if (requestContext is Activity && !requestContext.isFinishing && !requestContext.isDestroyed) {
            PermissionManager.showRequestPermissionWindow(requestContext)
            return
        }

        val settingsIntent = when {
            !PermissionManager.isAccessibilityServiceEnabled(appContext) -> {
                Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS)
            }

            !Settings.canDrawOverlays(appContext) -> {
                Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION).apply {
                    data = Uri.parse("package:${appContext.packageName}")
                }
            }

            else -> {
                Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS).apply {
                    data = Uri.parse("package:${appContext.packageName}")
                }
            }
        }.apply {
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }

        runCatching {
            appContext.startActivity(settingsIntent)
        }.onFailure { error ->
            Toast.makeText(appContext, "Please grant OpenGUI permissions in system settings.", Toast.LENGTH_LONG)
                .show()
            LogManager.saveLog(appContext, TAG, "Open permission settings failed: ${error.message}", -1)
        }
    }

    private fun startStandbyService(context: Context) {
        runCatching {
            StandbyForegroundService.start(context)
        }.onFailure { error ->
            LogManager.saveLog(context, TAG, "Start standby service failed: ${error.message}", -1)
        }
    }

    private fun startEventCollectors(context: Context) {
        if (eventJob == null) {
            eventJob = scope.launch {
                AutomationEventBus.events.collectLatest { event ->
                    when (event) {
                        is AutomationEvent.RemoteDispatch -> {
                            handleDispatch(
                                appContext = context,
                                executionId = event.executionId,
                                taskId = event.taskId,
                                taskName = event.taskName,
                                from = "standby dispatch",
                            )
                        }

                        is AutomationEvent.ReturnToPromotorApp,
                        is AutomationEvent.ErrorReturnToPromotorApp -> {
                            StandbyForegroundService.standbyManager?.reconnect()
                            showStandbyIsland("execution returned")
                        }

                        is AutomationEvent.AccessibilityServiceWarningEvent -> {
                            AIFloatWindowManager.hideExecuteTaskWindow("accessibility warning")
                            AIFloatWindowManager.getSlideExpandWindow()?.dismiss("accessibility warning")
                            AIFloatWindowManager.getAccessibilityServiceWarningWindow()?.show()
                        }

                        else -> Unit
                    }
                }
            }
        }

        if (socketStateJob == null) {
            socketStateJob = scope.launch {
                MessageController.executionConnectState.collectLatest { connected ->
                    if (connected == false && TaskCenter.executionId == null) {
                        delay(500)
                        StandbyForegroundService.standbyManager?.reconnect()
                        showStandbyIsland("execution socket idle")
                    }
                }
            }
        }
    }

    private fun handleDispatch(
        appContext: Context,
        executionId: Int,
        taskId: Int,
        taskName: String,
        from: String,
    ) {
        if (
            TaskCenter.executionId == executionId &&
            MessageController.hasActiveSocket()
        ) {
            LogManager.saveLog(appContext, TAG, "Duplicate dispatch ignored from $from", executionId)
            return
        }

        ensureWindows(appContext)
        ensureActionHandler(appContext)
        TaskCenter.reset(appContext, "Headless dispatch: $from")
        TaskCenter.taskId = taskId
        TaskCenter.taskTitle = taskName
        TaskCenter.executionId = executionId
        TaskCenter.currentTaskState = TaskCenter.TaskState.EXECUTE

        val actionHandler = MessageController.getActionHandler()
        if (actionHandler == null) {
            LogManager.saveLog(appContext, TAG, "No action handler for execution $executionId", executionId)
            return
        }

        AIFloatWindowManager.showStandbyIsland("Task running", from)
        MessageController.connectExecutionSocket(executionId.toLong(), actionHandler)
        StandbyForegroundService.standbyManager?.disconnect()
    }

    private fun ensureWindows(context: Context) {
        if (AIFloatWindowManager.getExecuteTaskWindow() == null) {
            ExecuteTaskWindow(context)
        }
        if (AIFloatWindowManager.getSlideExpandWindow() == null) {
            SlideExpandWindow(context)
        }
        if (AIFloatWindowManager.getCallUserWindow() == null) {
            CallUserWindow(context)
        }
        if (AIFloatWindowManager.getGradientWindow() == null) {
            GradientWindow(context)
        }
        if (AIFloatWindowManager.getAccessibilityServiceWarningWindow() == null) {
            AccessibilityServiceWarningWindow(context)
        }
    }

    private fun ensureActionHandler(context: Context) {
        if (MessageController.getActionHandler() != null) return
        val imageUploader = ImageUploaderImpl("", ServerConstant.getURL())
        val clickFeedbackView = ClickFeedbackView(context)
        val actionExecutor = ActionExecutor(context, imageUploader, clickFeedbackView)
        MessageController.init(
            context,
            actionExecutor,
            object : MessageController.TabCheckCallback {
                override fun onCheck(tabIndex: Int) = Unit
            },
        )
    }

    private fun currentDeviceId(context: Context): String {
        return Settings.Secure.getString(context.contentResolver, Settings.Secure.ANDROID_ID)
            ?: "unknown"
    }
}
