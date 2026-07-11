package com.coremate.opengui.feature.promotor.ui

import android.app.Activity
import android.app.AlertDialog
import android.os.Build
import android.os.Bundle
import android.text.InputType
import android.widget.EditText
import android.widget.Toast
import com.coremate.opengui.common.log.LogManager
import com.coremate.opengui.feature.promotor.runtime.HeadlessAgentRuntime

class VoiceCommandActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        HeadlessAgentRuntime.initialize(applicationContext)
        AIFloatWindowManager.showStandbyIsland("Type: 打开手机", "command entry")
        showCommandInput()
    }

    private fun showCommandInput() {
        if (isFinishing || (Build.VERSION.SDK_INT >= Build.VERSION_CODES.JELLY_BEAN_MR1 && isDestroyed)) return
        val input = EditText(this).apply {
            hint = "打开手机 帮我打开微信"
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_MULTI_LINE
            minLines = 2
            maxLines = 4
            setSingleLine(false)
        }
        val dialog = AlertDialog.Builder(this)
            .setTitle("OpenGUI command")
            .setMessage("Commands must include \"打开手机\" before they run.")
            .setView(input)
            .setPositiveButton("Run", null)
            .setNegativeButton("Cancel") { _, _ -> finish() }
            .setOnCancelListener { finish() }
            .create()
        dialog.setOnShowListener {
            dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener {
                val text = input.text?.toString()?.trim().orEmpty()
                if (text.isEmpty()) {
                    Toast.makeText(this, "Please enter a command.", Toast.LENGTH_SHORT).show()
                    return@setOnClickListener
                }
                if (submitCommand(text)) {
                    dialog.dismiss()
                    finish()
                }
            }
        }
        dialog.show()
    }

    private fun submitCommand(text: String): Boolean {
        val command = extractPhoneCommand(text)
        if (command == null) {
            Toast.makeText(this, "Command must include \"打开手机\".", Toast.LENGTH_SHORT).show()
            AIFloatWindowManager.showStandbyIsland("Need: 打开手机", "wake phrase missing")
            LogManager.saveLog(
                this,
                TAG,
                "Command ignored because wake phrase is missing: $text",
                -1,
            )
            return false
        }
        AIFloatWindowManager.showStandbyIsland("Submitting task...", "command entry")
        return HeadlessAgentRuntime.submitTextCommand(this, command)
    }

    private fun extractPhoneCommand(text: String): String? {
        val rawText = text.trim()
        if (!rawText.contains(WAKE_PHRASE)) return null
        return rawText.replaceFirst(WAKE_PHRASE, "").trim().ifBlank { rawText }
    }

    companion object {
        private const val TAG = "VoiceCommandActivity"
        private const val WAKE_PHRASE = "打开手机"
    }
}
