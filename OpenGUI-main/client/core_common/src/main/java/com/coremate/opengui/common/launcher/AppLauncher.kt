package com.coremate.opengui.common.launcher

import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.widget.Toast
import com.coremate.opengui.common_jvm.utils.Constants
import java.util.Locale

data class LaunchableApp(
    val appName: String,
    val packageName: String,
    val activityName: String?
)

/**
 * A singleton utility object for launching other applications.
 */
object AppLauncher {

    /**
     * Launches an application using its package name.
     *
     * @param context The context to use for launching the intent.
     * @param packageName The package name of the app to launch.
     * @return `true` if the app was launched successfully, `false` otherwise.
     */
    fun launchByPackageName(context: Context, packageName: String): Boolean {
        val launchIntent = context.packageManager.getLaunchIntentForPackage(packageName)
        if (launchIntent == null) {
            // App not found, show a toast or log an error
            Toast.makeText(context, "应用未安装: $packageName", Toast.LENGTH_SHORT).show()
            return false
        }

        // Add this flag if you are calling from a non-activity context (like a service)
        launchIntent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)

        context.startActivity(launchIntent)
        return true
    }

    /**
     * Checks if an application is installed.
     *
     * @param context The context to use.
     * @param packageName The package name to check.
     * @return `true` if the app is installed, `false` otherwise.
     */
    fun isAppInstalled(context: Context, packageName: String): Boolean {
        return try {
            context.packageManager.getPackageInfo(packageName, 0)
            true
        } catch (e: Exception) {
            false
        }
    }

    fun listLaunchableApps(context: Context): List<LaunchableApp> {
        val packageManager = context.packageManager
        val intent = Intent(Intent.ACTION_MAIN, null).apply {
            addCategory(Intent.CATEGORY_LAUNCHER)
        }

        @Suppress("DEPRECATION")
        val activities = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            packageManager.queryIntentActivities(
                intent,
                PackageManager.ResolveInfoFlags.of(0)
            )
        } else {
            packageManager.queryIntentActivities(intent, 0)
        }

        val seen = mutableSetOf<String>()
        return activities.mapNotNull { resolveInfo ->
            val packageName = resolveInfo.activityInfo?.packageName ?: return@mapNotNull null
            val activityName = resolveInfo.activityInfo?.name
            if (!seen.add("$packageName/$activityName")) return@mapNotNull null
            val label = resolveInfo.loadLabel(packageManager)?.toString()?.trim()
            if (label.isNullOrEmpty()) return@mapNotNull null
            LaunchableApp(
                appName = label,
                packageName = packageName,
                activityName = activityName
            )
        }.sortedWith(
            compareBy<LaunchableApp> { it.appName.lowercase(Locale.ROOT) }
                .thenBy { it.packageName }
        )
    }

    fun resolvePackageName(context: Context, appNameOrPackage: String?): String? {
        val query = appNameOrPackage?.trim().orEmpty()
        if (query.isEmpty()) return null

        if (looksLikePackageName(query) && isAppInstalled(context, query)) {
            return query
        }

        predefinedPackageName(query)?.let { return it }

        val normalizedQuery = normalizeAppName(query)
        val apps = listLaunchableApps(context)

        apps.firstOrNull {
            normalizeAppName(it.appName) == normalizedQuery
        }?.let { return it.packageName }

        apps.firstOrNull {
            normalizeAppName(it.appName).contains(normalizedQuery) ||
                    normalizedQuery.contains(normalizeAppName(it.appName))
        }?.let { return it.packageName }

        return getPackageNameFromAppName(context, query)
    }

    /**
     * Attempts to find the package name of an application given its human-readable name.
     * This is an heuristic and might not be perfectly accurate for all apps.
     *
     * @param context The context to use.
 * @param app Name The human-readable name of the app (e.g., "Douyin", "We Chat").
     * @return The package name if found, or null otherwise.
     */
    fun getPackageNameFromAppName(context: Context, appName: String): String? {
        val packageManager = context.packageManager
        val installedApplications = packageManager.getInstalledApplications(PackageManager.GET_META_DATA)

        // Prefer predefined constant mappings
        val predefinedPackageName = predefinedPackageName(appName)
        if (predefinedPackageName != null) {
            return predefinedPackageName
        }

        // Try scanning installed apps and fuzzy-match by app label; slower and less accurate
        for (app in installedApplications) {
            val appLabel = packageManager.getApplicationLabel(app).toString()
            if (appLabel.equals(appName, ignoreCase = true) || // Exact match.
                appLabel.contains(appName, ignoreCase = true)) { // Contains match.
                return app.packageName
            }
        }
        return null
    }

    private fun predefinedPackageName(appName: String): String? {
        return when (normalizeAppName(appName)) {
            "抖音" -> Constants.AppPackageNames.DOUYIN
            "微信" -> Constants.AppPackageNames.WECHAT
            "qq" -> Constants.AppPackageNames.QQ
            "小红书" -> Constants.AppPackageNames.XIAOHONGSHU
            "淘宝" -> Constants.AppPackageNames.TAOBAO
            "京东" -> Constants.AppPackageNames.JD
            "支付宝" -> Constants.AppPackageNames.ALIPAY
            "微博" -> Constants.AppPackageNames.SINEWEIBO
            "哔哩哔哩", "bilibili" -> Constants.AppPackageNames.bilibili
            "qq音乐" -> Constants.AppPackageNames.qqmusic
            "网易云音乐" -> "com.netease.cloudmusic"
            else -> null
        }
    }

    private fun looksLikePackageName(value: String): Boolean {
        return value.contains(".") && value.all {
            it.isLetterOrDigit() || it == '_' || it == '.'
        }
    }

    private fun normalizeAppName(value: String): String {
        return value
            .trim()
            .lowercase(Locale.ROOT)
            .replace(" ", "")
            .removeSuffix("app")
            .removeSuffix("应用")
            .removeSuffix("软件")
    }
}
