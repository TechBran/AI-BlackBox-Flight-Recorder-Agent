/**
 * notifications.js
 * Browser push notifications, vibration, and task completion alerts
 *
 * Source: Portal/app.js lines 3177-3305, 4495-4506
 * Refactored: 2025-12-20
 */

import { $, toast } from './core-utils.js';
import { getOperator } from './state-management.js';

// =============================================================================
// Vibration Support
// =============================================================================

/**
 * Vibrate device (works on Android)
 * @param {number[]} pattern - Vibration pattern in milliseconds
 */
export function vibrate(pattern = [200]) {
    if ("vibrate" in navigator) {
        try {
            navigator.vibrate(pattern);
            console.log("[Vibration] Triggered:", pattern);
        } catch (e) {
            console.log("[Vibration] Error:", e);
        }
    } else {
        console.log("[Vibration] Not supported");
    }
}

// =============================================================================
// Notification Display
// =============================================================================

/**
 * Show browser notification (with Android native support)
 * @param {string} title - Notification title
 * @param {Object} options - Notification options
 * @param {string} [options.body] - Notification body text
 * @param {string} [options.tag] - Notification tag for grouping
 * @param {boolean} [options.silent] - Whether to play sound
 * @param {string} [options.operator] - Operator for filtering
 * @returns {Notification|undefined} The notification object if created
 */
export function showNotification(title, options = {}) {
    const currentOperator = getOperator();
    console.log('[DEBUG] showNotification called:', { title, options, androidBridge: typeof AndroidBridge, currentOperator });

    // Check operator filtering - only notify if task belongs to current operator.
    // A page with no resolved operator yet filters nothing (it owns every task).
    if (options.operator && currentOperator && options.operator !== currentOperator) {
        console.log(`[Notifications] Skipping - task belongs to ${options.operator}, current operator is ${currentOperator}`);
        return;
    }

    // PRIORITY 1: Use Android native notifications if available
    if (typeof AndroidBridge !== 'undefined' && typeof AndroidBridge.showNotification === 'function') {
        try {
            console.log('[Notifications] Using Android native bridge');
            console.log('[Notifications] Calling: AndroidBridge.showNotification("' + title + '", "' + (options.body || '') + '")');
            AndroidBridge.showNotification(title, options.body || '');
            console.log('[Notifications] Android notification sent successfully');
            return;
        } catch (err) {
            console.error('[Notifications] Android bridge error:', err);
            // Fall through to web notifications
        }
    } else {
        console.log('[Notifications] AndroidBridge check:', {
            exists: typeof AndroidBridge !== 'undefined',
            hasMethod: typeof AndroidBridge !== 'undefined' && typeof AndroidBridge.showNotification === 'function',
            isAndroidWrapper: window.isAndroidWebWrapper
        });
        console.log('[Notifications] AndroidBridge not available, using web notifications');
    }

    // PRIORITY 2: Use Web Notifications API
    if (!("Notification" in window)) {
        console.log("[Notifications] Not supported, falling back to toast");
        toast(options.body || title);
        return;
    }

    if (Notification.permission === "granted") {
        const notification = new Notification(title, {
            icon: '/favicon.ico',
            badge: '/favicon.ico',
            requireInteraction: false,
            vibrate: [200, 100, 200],
            ...options
        });

        setTimeout(() => notification.close(), 10000);

        notification.onclick = () => {
            window.focus();
            notification.close();
        };

        return notification;
    } else {
        // Permission not granted or denied, use toast
        console.log('[Notifications] Web notifications not available (permission:', Notification.permission, '), using toast');
        const toastMessage = options.body || title;
        console.log('[DEBUG] Calling toast() with message:', toastMessage);
        toast(toastMessage);
        console.log('[DEBUG] toast() returned');
    }
}

// =============================================================================
// High-Level Notification Functions
// =============================================================================

/**
 * Combined notification for new messages
 * @param {string} [messagePreview] - Preview of the message
 */
export function notifyNewMessage(messagePreview) {
    // Vibrate (short pulse)
    vibrate([200]);

    // Show notification if permission granted
    showNotification("New Message", {
        body: messagePreview || "You have a new response",
        tag: "new-message",
        silent: false
    });
}

/**
 * Combined notification for task completion
 * @param {string} taskType - Type of task (e.g., "image", "video", "music")
 * @param {boolean} [success=true] - Whether the task succeeded
 * @param {string} [taskOperator=null] - Operator who initiated the task
 */
export function notifyTaskComplete(taskType, success = true, taskOperator = null) {
    console.log('[DEBUG] notifyTaskComplete called:', { taskType, success, taskOperator, currentOperator: getOperator() });

    if (success) {
        // Success vibration (two short pulses)
        vibrate([100, 50, 100]);

        console.log('[DEBUG] About to call showNotification for success');
        showNotification("Task Complete", {
            body: `Your ${taskType} is ready!`,
            tag: "task-complete",
            silent: false,
            operator: taskOperator
        });
        console.log('[DEBUG] showNotification called for success');
    } else {
        // Failure vibration (longer pulse)
        vibrate([400]);

        console.log('[DEBUG] About to call showNotification for failure');
        showNotification("Task Failed", {
            body: `Your ${taskType} task failed`,
            operator: taskOperator,
            tag: "task-failed",
            silent: false
        });
        console.log('[DEBUG] showNotification called for failure');
    }
}

// =============================================================================
// Initialization
// =============================================================================

/**
 * Initialize notification system
 */
export function initNotificationSystem() {
    // Check if notifications are supported and permission status
    if (!("Notification" in window)) {
        console.log('[Notifications] Not supported in this browser');
        return;
    }

    console.log('[Notifications] System initialized, permission:', Notification.permission);

    // Don't auto-request - must be from user gesture
    // Will request on first task generation instead
}

/**
 * Request notification permission (must be called from user gesture)
 * @returns {Promise<NotificationPermission>} Permission result
 */
export async function requestNotificationPermission() {
    if (!("Notification" in window)) {
        return "denied";
    }

    if (Notification.permission === "granted") {
        return "granted";
    }

    if (Notification.permission !== "denied") {
        const permission = await Notification.requestPermission();
        console.log('[Notifications] Permission requested:', permission);
        return permission;
    }

    return Notification.permission;
}
