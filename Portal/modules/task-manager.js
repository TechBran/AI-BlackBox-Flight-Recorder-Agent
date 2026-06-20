/**
 * task-manager.js
 * Background task processing, polling, and async generation functions
 * Handles image/video/audio generation tasks with progress tracking
 *
 * Source: Portal/app.js lines 2468-3125
 * Refactored: 2025-12-20
 */

import { $, toast } from './core-utils.js';
import { getOperator } from './state-management.js';
import { notifyTaskComplete } from './notifications.js';

// =============================================================================
// TaskManager Class
// =============================================================================

/**
 * TaskManager - manages background tasks with polling and notification
 */
export class TaskManager {
    constructor() {
        this.activeTasks = new Map(); // task_id -> task info
        this.pollInterval = 3000; // Poll every 3 seconds
        this.addBubble = null; // Set by init to avoid circular deps
        this.attachGeneratedAudioPlayer = null; // Set by init
    }

    /**
     * Initialize task manager with dependencies
     * @param {Function} addBubbleFn - Function to add chat bubbles
     * @param {Function} attachAudioPlayerFn - Function to attach audio players
     */
    init(addBubbleFn, attachAudioPlayerFn) {
        this.addBubble = addBubbleFn;
        this.attachGeneratedAudioPlayer = attachAudioPlayerFn;
        this.loadActiveTasks();
        this.startPolling();
    }

    /**
     * Load active tasks from localStorage on page load
     */
    loadActiveTasks() {
        const currentOperator = getOperator() || 'unknown';
        console.log(`[TaskManager] Loading tasks for operator: ${currentOperator}`);

        const stored = localStorage.getItem('bb_active_tasks');
        if (stored) {
            try {
                const tasks = JSON.parse(stored);
                tasks.forEach(task => {
                    // New format: {taskId, operator}
                    if (typeof task === 'object' && task.taskId) {
                        if (task.operator === currentOperator || task.operator === 'unknown') {
                            console.log(`[TaskManager] Resuming task ${task.taskId} for operator ${task.operator}`);
                            this.resumeTask(task.taskId, task.operator);
                        } else {
                            console.log(`[TaskManager] Skipping task ${task.taskId} - belongs to ${task.operator}, current is ${currentOperator}`);
                        }
                    } else if (typeof task === 'string') {
                        // Legacy format
                        console.log(`[TaskManager] Resuming legacy task ${task} as ${currentOperator}`);
                        this.resumeTask(task, currentOperator);
                    }
                });
            } catch (e) {
                console.error('Failed to load active tasks:', e);
                localStorage.removeItem('bb_active_tasks');
            }
        }
    }

    /**
     * Save active tasks to localStorage
     */
    saveActiveTasks() {
        const tasksWithOperators = [];
        for (const [taskId, taskInfo] of this.activeTasks.entries()) {
            tasksWithOperators.push({
                taskId: taskId,
                operator: taskInfo.operator || 'unknown'
            });
        }
        localStorage.setItem('bb_active_tasks', JSON.stringify(tasksWithOperators));
    }

    /**
     * Add a new task to track
     * @param {string} taskId - Task ID
     * @param {string} type - Task type
     * @param {string} prompt - Task prompt
     * @param {string|Object} operatorOrOptions - Operator name OR video options {duration, resolution}
     * @param {number} count - Number of items being generated (for image generation)
     */
    addTask(taskId, type, prompt, operatorOrOptions = null, count = 1) {
        console.log('[DEBUG] ===== TaskManager.addTask CALLED =====');

        // Handle video options passed as 4th param (from video_task events)
        let taskOperator;
        let videoOptions = {};
        if (type === 'video_generation' && typeof operatorOrOptions === 'object' && operatorOrOptions !== null) {
            // Video tasks pass {duration, resolution} as 4th param
            videoOptions = operatorOrOptions;
            taskOperator = getOperator() || 'unknown';
        } else {
            taskOperator = operatorOrOptions || getOperator() || 'unknown';
        }

        console.log('[DEBUG] Adding task:', { taskId, type, operator: taskOperator, prompt: prompt?.substring(0, 50), count });

        this.activeTasks.set(taskId, {
            type: type,
            prompt: prompt,
            operator: taskOperator,
            startTime: Date.now()
        });

        console.log('[DEBUG] Active tasks count:', this.activeTasks.size);
        this.saveActiveTasks();
        this.showTaskNotification(taskId, type, 'queued', 0, null, taskOperator);

        // Show placeholder in chat for image/video/music generation
        if (type === 'image_generation') {
            showImagePlaceholder(taskId, prompt, count);
        } else if (type === 'video_generation') {
            showVideoPlaceholder(taskId, prompt, videoOptions);
        } else if (type === 'lyria_music') {
            showMusicPlaceholder(taskId, prompt, count);
        }
    }

    /**
     * Remove a task from tracking
     * @param {string} taskId - Task ID
     */
    removeTask(taskId) {
        this.activeTasks.delete(taskId);
        this.saveActiveTasks();
    }

    /**
     * Resume tracking an existing task
     * @param {string} taskId - Task ID
     * @param {string} storedOperator - Stored operator
     */
    async resumeTask(taskId, storedOperator = null) {
        console.log(`[TaskManager] Resuming task: ${taskId}, operator: ${storedOperator}`);
        const taskOperator = storedOperator || getOperator() || 'unknown';

        try {
            const status = await this.checkTaskStatus(taskId);
            if (status.status === 'completed' || status.status === 'failed') {
                this.activeTasks.set(taskId, {
                    type: status.task_type,
                    prompt: null,
                    operator: taskOperator,
                    startTime: Date.now()
                });
                this.handleTaskComplete(taskId, status);
            } else {
                this.activeTasks.set(taskId, {
                    type: status.task_type,
                    prompt: null,
                    operator: taskOperator,
                    startTime: Date.now()
                });
                this.showTaskNotification(taskId, status.task_type, status.status, status.progress, null, taskOperator);
            }
        } catch (e) {
            console.error(`[TaskManager] Failed to resume task ${taskId}:`, e);
            this.removeTask(taskId);
        }
    }

    /**
     * Start the polling loop
     */
    startPolling() {
        setInterval(() => {
            this.pollActiveTasks();
        }, this.pollInterval);
    }

    /**
     * Poll all active tasks for status updates
     */
    async pollActiveTasks() {
        console.log('[DEBUG] TaskManager.pollActiveTasks called, active tasks:', this.activeTasks.size);

        if (this.activeTasks.size === 0) {
            console.log('[DEBUG] No active tasks to poll');
            return;
        }

        for (const [taskId, taskInfo] of this.activeTasks.entries()) {
            console.log('[DEBUG] Polling task:', taskId, taskInfo.type);
            try {
                const status = await this.checkTaskStatus(taskId);
                console.log('[DEBUG] Task status:', status.status);

                if (status.status === 'completed' || status.status === 'failed') {
                    console.log('[DEBUG] Task finished, calling handleTaskComplete');
                    this.handleTaskComplete(taskId, status);
                    this.removeTask(taskId);
                } else {
                    this.updateTaskProgress(taskId, status);
                }
            } catch (e) {
                console.error(`[TaskManager] Error polling task ${taskId}:`, e);
            }
        }
    }

    /**
     * Check task status from server
     * @param {string} taskId - Task ID
     * @returns {Promise<Object>} Task status
     */
    async checkTaskStatus(taskId) {
        const response = await fetch(`/tasks/status/${taskId}`);
        if (!response.ok) {
            throw new Error(`Task status check failed: ${response.statusText}`);
        }
        return await response.json();
    }

    /**
     * Handle task completion
     * @param {string} taskId - Task ID
     * @param {Object} status - Task status
     */
    handleTaskComplete(taskId, status) {
        console.log(`[TaskManager] Task ${taskId} completed:`, status);
        console.log('[DEBUG] ===== TASK COMPLETION DETECTED =====');
        console.log('[DEBUG] Task type:', status.task_type);
        console.log('[DEBUG] Status:', status.status);

        const storedTask = this.activeTasks.get(taskId);
        const taskOperator = storedTask?.operator || status.result_data?.operator || null;
        console.log('[DEBUG] Task operator:', taskOperator);

        const currentOperator = getOperator() || 'unknown';
        const isOwnTask = !taskOperator || taskOperator === currentOperator || taskOperator === 'unknown';

        if (status.status === 'completed') {
            this.showTaskNotification(taskId, status.task_type, 'completed', 100, null, taskOperator);

            const taskTypeName = status.task_type.replace(/_/g, ' ');
            console.log('[DEBUG] Calling notifyTaskComplete with:', { taskTypeName, taskOperator });
            notifyTaskComplete(taskTypeName, true, taskOperator);
            console.log('[DEBUG] notifyTaskComplete returned');

            if (isOwnTask) {
                if (status.task_type === 'image_generation' && status.result_url) {
                    this.displayImageResult(status.result_url, taskId, status.result_data);
                } else if (status.task_type === 'video_generation' && status.result_url) {
                    this.displayVideoResult(status.result_url, taskId);
                } else if (status.task_type === 'audio_analysis' && status.result_data) {
                    this.displayAudioAnalysisResult(status.result_data);
                } else if ((status.task_type === 'google_tts' || status.task_type === 'gemini_tts' || status.task_type === 'lyria_music') && status.result_url) {
                    this.displayAudioResult(status.result_url, status.task_type, taskId);
                } else if ((status.task_type === 'browser_use' || status.task_type === 'use_computer') && status.result_data) {
                    this.displayBrowserResult(status.result_data, taskId);
                }
            } else {
                console.log(`[TaskManager] Skipping result display for task ${taskId} - belongs to ${taskOperator}, current is ${currentOperator}`);
            }
        } else if (status.status === 'failed') {
            this.showTaskNotification(taskId, status.task_type, 'failed', 0, status.error_message, taskOperator);

            if (isOwnTask) {
                toast(`Task failed: ${status.error_message || 'Unknown error'}`);
            }

            const taskTypeName = status.task_type.replace(/_/g, ' ');
            notifyTaskComplete(taskTypeName, false, taskOperator);
        }
    }

    /**
     * Update task progress UI
     * @param {string} taskId - Task ID
     * @param {Object} status - Task status
     */
    updateTaskProgress(taskId, status) {
        const progressEl = document.getElementById(`task-progress-${taskId}`);
        if (progressEl) {
            progressEl.textContent = `${status.progress}%`;
            progressEl.style.width = `${status.progress}%`;
        }
    }

    /**
     * Show task notification toast
     */
    showTaskNotification(taskId, type, status, progress = 0, error = null, taskOperator = null) {
        const currentOperator = getOperator() || 'unknown';
        if (taskOperator && taskOperator !== currentOperator && taskOperator !== 'unknown') {
            console.log(`[TaskManager] Skipping notification for task ${taskId} - belongs to ${taskOperator}, current is ${currentOperator}`);
            return;
        }

        const typeNames = {
            'image_generation': 'Image Generation',
            'video_generation': 'Video Generation (Veo 3.1)',
            'audio_analysis': 'Audio Analysis',
            'google_tts': 'Google TTS',
            'gemini_tts': 'Gemini TTS',
            'lyria_music': 'Music Generation (Lyria)',
            'browser_use': 'Computer Use',
            'use_computer': 'Computer Use'
        };

        const typeName = typeNames[type] || type;
        let message = '';

        if (status === 'queued' || status === 'pending') {
            message = `${typeName} queued...`;
        } else if (status === 'processing') {
            message = `${typeName} in progress... ${progress}%`;
        } else if (status === 'completed') {
            message = `${typeName} completed!`;
        } else if (status === 'failed') {
            message = `${typeName} failed: ${error || 'Unknown error'}`;
        }

        toast(message);
    }

    /**
     * Display image result in chat
     * Replaces the placeholder bubble if found, otherwise creates new bubble
     * @param {string} url - Image URL (single or from all_urls array)
     * @param {string} taskId - Task ID to find placeholder
     * @param {Object} resultData - Full result data (may contain all_urls for multiple images)
     */
    displayImageResult(url, taskId = null, resultData = null) {
        // Build image HTML - support multiple images
        let imageUrls = [url];
        if (resultData && resultData.all_urls && resultData.all_urls.length > 0) {
            imageUrls = resultData.all_urls;
        }

        const imageHtml = imageUrls.map(imgUrl =>
            `<img src="${imgUrl}" alt="Generated Image" class="generated-image" style="max-width: 100%; border-radius: 8px; margin: 4px 0;">`
        ).join('\n');

        // Try to find and replace the placeholder bubble
        if (taskId) {
            const placeholderBubble = document.querySelector(`.bubble.generating-image[data-task-id="${taskId}"]`);
            if (placeholderBubble) {
                // Remove the generating-image class
                placeholderBubble.classList.remove('generating-image');

                const bubbleText = placeholderBubble.querySelector('.bubble-text');
                if (bubbleText) {
                    // Replace placeholder content with actual image(s)
                    bubbleText.innerHTML = imageHtml;

                    // Add download buttons to images
                    setTimeout(() => {
                        bubbleText.querySelectorAll('img').forEach(img => {
                            import('./chat-bubbles.js').then(module => {
                                module.addDownloadButtonToImage(img, placeholderBubble);
                            });
                        });
                    }, 0);

                    console.log(`[TaskManager] Replaced placeholder with ${imageUrls.length} image(s) for task ${taskId}`);
                    return;
                }
            }
        }

        // Fallback: no placeholder found, create new bubble
        if (this.addBubble) {
            this.addBubble("assistant", imageHtml);
        }
    }

    /**
     * Display video result in chat
     * Replaces the placeholder bubble if found, otherwise creates new bubble
     * Note: Extend button is added by MutationObserver in chat-send.js
     * @param {string} url - Video URL
     * @param {string} taskId - Task ID to find placeholder
     */
    displayVideoResult(url, taskId = null) {
        // Video HTML - preload metadata shows first frame as preview, no autoplay
        const videoHtml = `
            <video controls preload="metadata" src="${url}" class="generated-video" style="max-width: 100%; border-radius: 8px; margin: 8px 0;"></video>
        `;

        let targetBubble = null;

        // Try to find and replace the placeholder bubble
        if (taskId) {
            const placeholderBubble = document.querySelector(`.bubble.generating-video[data-task-id="${taskId}"]`);
            if (placeholderBubble) {
                // Remove the generating-video class
                placeholderBubble.classList.remove('generating-video');

                const bubbleText = placeholderBubble.querySelector('.bubble-text');
                if (bubbleText) {
                    // Replace placeholder content with actual video
                    bubbleText.innerHTML = videoHtml;
                    targetBubble = placeholderBubble;
                    console.log(`[TaskManager] Replaced video placeholder for task ${taskId}`);
                }
            }
        }

        // Fallback: no placeholder found, create new bubble
        if (!targetBubble && this.addBubble) {
            targetBubble = this.addBubble("assistant", videoHtml);
        }

        // Persist to history so video survives refresh
        if (targetBubble) {
            this.persistBubbleToHistory(targetBubble);
        }
    }

    /**
     * Persist a bubble's content to localStorage history
     * @param {HTMLElement} bubble - The bubble element to persist
     */
    persistBubbleToHistory(bubble) {
        if (!bubble) return;

        try {
            // Import state management and save
            import('./state-management.js').then(({ getHistoryData, setHistoryData, saveHistory }) => {
                const contentDiv = bubble.querySelector('.response-content') ||
                                   bubble.querySelector('.bubble-text') ||
                                   bubble.querySelector('span');
                if (!contentDiv) return;

                const updatedContent = contentDiv.innerHTML;
                const hist = document.getElementById('history');
                if (!hist) return;

                const allBubbles = Array.from(hist.children);
                const bubbleIndex = allBubbles.indexOf(bubble);

                // Count assistant bubbles up to this one
                let assistantCount = 0;
                for (let i = 0; i <= bubbleIndex && i < allBubbles.length; i++) {
                    if (allBubbles[i].classList.contains('assistant')) {
                        assistantCount++;
                    }
                }

                const historyData = getHistoryData();
                let historyAssistantCount = 0;
                for (let i = 0; i < historyData.length; i++) {
                    if (historyData[i].role === 'assistant') {
                        historyAssistantCount++;
                        if (historyAssistantCount === assistantCount) {
                            historyData[i].content = updatedContent;
                            setHistoryData(historyData);
                            saveHistory();
                            console.log('[TaskManager] Persisted video to history');
                            return;
                        }
                    }
                }

                // If bubble is new (from hamburger menu), add to history
                if (assistantCount > historyAssistantCount) {
                    historyData.push({
                        role: 'assistant',
                        content: updatedContent
                    });
                    setHistoryData(historyData);
                    saveHistory();
                    console.log('[TaskManager] Added new video bubble to history');
                }
            });
        } catch (err) {
            console.error('[TaskManager] Failed to persist bubble:', err);
        }
    }

    /**
     * Display audio result in chat
     * Replaces the placeholder bubble if found (for lyria_music), otherwise creates new bubble
     * @param {string} url - Audio URL
     * @param {string} taskType - Task type
     * @param {string} taskId - Task ID to find placeholder
     */
    displayAudioResult(url, taskType = 'audio', taskId = null) {
        const isMusic = url.includes('_music.wav') || taskType === 'lyria_music';
        const label = isMusic ? '🎵 Generated Music' : '🔊 Generated Audio';

        let targetBubble = null;

        // Try to find and replace the music placeholder bubble
        if (isMusic && taskId) {
            const placeholderBubble = document.querySelector(`.bubble.generating-music[data-task-id="${taskId}"]`);
            if (placeholderBubble) {
                // Remove the generating-music class
                placeholderBubble.classList.remove('generating-music');

                const bubbleText = placeholderBubble.querySelector('.bubble-text');
                if (bubbleText) {
                    // Replace placeholder content with audio result
                    bubbleText.innerHTML = `<div class="audio-result"><span class="audio-label">${label}</span></div>`;
                    bubbleText.style.cssText = ''; // Reset any inline styles
                    targetBubble = placeholderBubble;
                    console.log(`[TaskManager] Replaced music placeholder for task ${taskId}`);
                }
            }
        }

        // Fallback: no placeholder found, create new bubble
        if (!targetBubble && this.addBubble) {
            targetBubble = this.addBubble("assistant", `<div class="audio-result"><span class="audio-label">${label}</span></div>`);
        }

        // Attach audio player to the bubble
        if (targetBubble && this.attachGeneratedAudioPlayer) {
            this.attachGeneratedAudioPlayer(targetBubble, url, isMusic ? 'generated-music' : 'generated-audio');
        }
    }

    /**
     * Display audio analysis result in chat
     * @param {Object} resultData - Analysis result
     */
    displayAudioAnalysisResult(resultData) {
        const transcript = resultData.transcript || JSON.stringify(resultData, null, 2);
        if (this.addBubble) {
            this.addBubble("assistant", `**Audio Analysis Result:**\n\n${transcript}`);
        }
    }

    /**
     * Display Computer Use result in chat
     * @param {Object} resultData - Browser result with screenshots and summary
     * @param {string} taskId - Task ID
     */
    displayBrowserResult(resultData, taskId = null) {
        const resultText = resultData.result_text || 'Browser task completed.';
        const screenshots = resultData.screenshots || [];
        const finalScreenshot = resultData.final_screenshot;
        const steps = resultData.steps || 0;
        const tokens = resultData.tokens || {};

        let html = `<div class="browser-result">`;
        html += `<div class="browser-result-header">`;
        html += `<span class="browser-badge"><svg class="browser-badge-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8M12 17v4"/></svg>Computer Use</span>`;
        html += `<span style="font-size:0.8rem;opacity:0.8">${steps} steps</span>`;
        html += `</div>`;

        // Final screenshot
        if (finalScreenshot) {
            html += `<img class="browser-screenshot" src="${finalScreenshot}" alt="Final browser screenshot" onclick="window.open('${finalScreenshot}', '_blank')" />`;
        }

        // Summary text
        html += `<div class="browser-result-summary">${resultText}</div>`;

        // Screenshot gallery (if more than 1)
        if (screenshots.length > 1) {
            html += `<div class="browser-gallery">`;
            screenshots.forEach((url, i) => {
                html += `<img class="browser-gallery-thumb" src="${url}" alt="Step ${i}" onclick="window.open('${url}', '_blank')" />`;
            });
            html += `</div>`;
        }

        // Steps info
        html += `<div class="browser-steps-info">`;
        html += `<span>${screenshots.length} screenshot${screenshots.length !== 1 ? 's' : ''}</span>`;
        if (tokens.input || tokens.output) {
            html += `<span>${(tokens.input || 0).toLocaleString()} in / ${(tokens.output || 0).toLocaleString()} out tokens</span>`;
        }
        html += `</div>`;
        html += `</div>`;

        // Find placeholder or add new bubble
        let targetBubble = null;
        if (taskId) {
            const placeholder = document.querySelector(`[data-task-id="${taskId}"]`);
            if (placeholder) {
                targetBubble = placeholder.closest('.msg-bubble');
                if (targetBubble) {
                    placeholder.outerHTML = html;
                }
            }
        }

        if (!targetBubble && this.addBubble) {
            this.addBubble("assistant", html, { isHTML: true });
        }
    }
}

// =============================================================================
// Singleton Instance
// =============================================================================

/** Global task manager instance */
export const taskManager = new TaskManager();

// =============================================================================
// Async Generation Functions
// =============================================================================

/**
 * Queue image generation with Nano Banana Pro options
 * @param {string} prompt - Image prompt
 * @param {Object} options - Generation options
 * @param {string} options.aspectRatio - Aspect ratio (1:1, 16:9, 9:16, etc.)
 * @param {string} options.resolution - Resolution (1K, 2K, 4K)
 * @param {number} options.numberOfImages - Number of images to generate (1-4)
 * @param {Array} options.referenceImages - Reference images for style guidance
 * @returns {Promise<string>} Task ID
 */
export async function generateImageAsync(prompt, options = {}) {
    console.log('[DEBUG] ===== generateImageAsync CALLED =====');
    console.log('[DEBUG] Prompt:', prompt);
    console.log('[DEBUG] Options:', options);

    try {
        const operator = getOperator();
        console.log('[DEBUG] Operator:', operator);

        // Build request payload. Provider-aware: pull reference images out for
        // special handling, then spread the remaining catalog params verbatim so
        // their names match the catalog / GenIn (aspectRatio/resolution/
        // numberOfImages/size/quality) regardless of which provider supplied them.
        const { referenceImages, ...params } = options;
        const payload = {
            prompt,
            operator,
            ...params
        };

        // Add reference images if provided (gemini-only image-to-image, v1)
        if (referenceImages && referenceImages.length > 0) {
            payload.referenceImages = referenceImages;
        }

        const response = await fetch('/generate/image', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });

        console.log('[DEBUG] Response status:', response.status);

        if (!response.ok) {
            const err = await response.json();
            throw new Error(err.detail || `Image generation failed: ${response.statusText}`);
        }

        const data = await response.json();
        console.log('[DEBUG] Got task_id:', data.task_id);

        taskManager.addTask(data.task_id, 'image_generation', prompt);

        // Show placeholder in chat
        showImagePlaceholder(data.task_id, prompt, options.numberOfImages || 1);

        console.log('[DEBUG] taskManager.addTask returned successfully');

        const imgCount = options.numberOfImages || 1;
        // resolution (gemini) / size (openai) -- whichever the provider exposes
        const qualInfo = options.resolution || options.size || options.quality;
        const suffix = qualInfo ? ` @ ${qualInfo}` : '';
        toast(`Image generation queued (${imgCount} image${imgCount > 1 ? 's' : ''}${suffix})...`);

        return data.task_id;
    } catch (error) {
        console.error('Image generation error:', error);
        toast(`Image generation failed: ${error.message}`);
        throw error;
    }
}

/**
 * Show a minimal placeholder animation while image is generating
 * Clean, professional style - just icon with animation
 * @param {string} taskId - Task ID
 * @param {string} prompt - Image prompt
 * @param {number} count - Number of images being generated
 */
function showImagePlaceholder(taskId, prompt, count = 1) {
    const hist = document.getElementById('history');
    if (!hist) return;

    // Create minimal bubble with just icon animation
    const wrap = document.createElement('div');
    wrap.className = 'bubble assistant generating-image';
    wrap.dataset.taskId = taskId;

    const span = document.createElement('span');
    span.className = 'bubble-text';
    span.style.cssText = 'display: flex; align-items: center; gap: 8px; font-size: 14px; color: #888;';

    // Image icon
    const icon = document.createElement('span');
    icon.textContent = '🖼️';
    icon.style.cssText = 'font-size: 20px;';

    // Generating text
    const text = document.createElement('span');
    text.textContent = count > 1 ? `Generating ${count} images` : 'Generating image';

    // Animated dots
    const dots = document.createElement('span');
    dots.className = 'thinking-dots';
    dots.innerHTML = '<span></span><span></span><span></span>';

    span.appendChild(icon);
    span.appendChild(text);
    span.appendChild(dots);
    wrap.appendChild(span);

    hist.appendChild(wrap);

    // Scroll to bottom
    hist.scrollTop = hist.scrollHeight;
}

/**
 * Queue video generation (Veo 3.1 with full options)
 * @param {string} prompt - Video prompt
 * @param {Object} imageData - Optional image data for image-to-video
 * @param {Object} options - Generation options
 * @param {string} options.aspectRatio - Aspect ratio (16:9 or 9:16)
 * @param {number} options.duration - Duration in seconds (4, 6, or 8)
 * @param {string} options.resolution - Resolution (720p or 1080p)
 * @param {string} options.negativePrompt - Things to exclude from video
 * @returns {Promise<string>} Task ID
 */
export async function generateVideoAsync(prompt, imageData = null, options = {}) {
    console.log('[DEBUG] ===== generateVideoAsync CALLED =====');
    console.log('[DEBUG] Prompt:', prompt);
    console.log('[DEBUG] Options:', options);
    console.log('[DEBUG] Has image data:', !!imageData);

    try {
        const operator = getOperator();
        console.log('[DEBUG] Operator:', operator);

        // Build request payload with Veo 3.1 options
        const payload = {
            prompt,
            operator,
            aspectRatio: options.aspectRatio || '16:9',
            duration: options.duration || 8,
            resolution: options.resolution || '720p'
        };

        // Add negative prompt if provided
        if (options.negativePrompt) {
            payload.negativePrompt = options.negativePrompt;
        }

        // Add image data for image-to-video
        if (imageData) {
            payload.image = {
                data: imageData.data,
                mime_type: imageData.mimeType
            };
        }

        console.log('[DEBUG] Sending payload to /generate/video');

        const response = await fetch('/generate/video', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });

        console.log('[DEBUG] Response status:', response.status);

        if (!response.ok) {
            const err = await response.json();
            throw new Error(err.detail || `Video generation failed: ${response.statusText}`);
        }

        const data = await response.json();
        console.log('[DEBUG] Got task_id:', data.task_id);

        taskManager.addTask(data.task_id, 'video_generation', prompt);

        // Show placeholder in chat
        showVideoPlaceholder(data.task_id, prompt, options);

        console.log('[DEBUG] taskManager.addTask returned successfully');

        const mode = imageData ? 'image-to-video' : 'text-to-video';
        const durationStr = options.duration || 8;
        const resStr = options.resolution || '720p';
        toast(`Video (${mode}, ${durationStr}s @ ${resStr}) queued (5-20 min)...`);

        return data.task_id;
    } catch (error) {
        console.error('Video generation error:', error);
        toast(`Video generation failed: ${error.message}`);
        throw error;
    }
}

/**
 * Show a placeholder animation in chat while video is generating
 * @param {string} taskId - Task ID
 * @param {string} prompt - Video prompt
 * @param {Object} options - Video options (for display)
 */
function showVideoPlaceholder(taskId, prompt, options = {}) {
    const hist = document.getElementById('history');
    if (!hist) return;

    // Create bubble similar to thinking bubble
    const wrap = document.createElement('div');
    wrap.className = 'bubble assistant generating-video';
    wrap.dataset.taskId = taskId;

    const span = document.createElement('span');
    span.className = 'bubble-text';

    // Build info string
    const duration = options.duration || 8;
    const resolution = options.resolution || '720p';
    const aspectRatio = options.aspectRatio || '16:9';
    const infoStr = `${duration}s • ${resolution} • ${aspectRatio}`;

    const textSpan = document.createElement('span');
    textSpan.className = 'generating-message';
    textSpan.innerHTML = `<span class="video-gen-icon">🎬</span> Generating video <span class="video-gen-info">(${infoStr})</span>`;

    const dots = document.createElement('span');
    dots.className = 'thinking-dots';
    dots.innerHTML = '<span></span><span></span><span></span>';

    // Add estimate text
    const estimate = document.createElement('span');
    estimate.className = 'video-gen-estimate';
    estimate.textContent = 'Veo 3.1 • 5-20 minutes';

    span.appendChild(textSpan);
    span.appendChild(dots);
    span.appendChild(document.createElement('br'));
    span.appendChild(estimate);
    wrap.appendChild(span);

    hist.appendChild(wrap);

    // Scroll to bottom
    hist.scrollTop = hist.scrollHeight;
}

/**
 * Show a placeholder animation in chat while music is generating
 * @param {string} taskId - Task ID
 * @param {string} prompt - Music prompt
 * @param {number} sampleCount - Number of variations being generated
 */
function showMusicPlaceholder(taskId, prompt, sampleCount = 1) {
    const hist = document.getElementById('history');
    if (!hist) return;

    // Create bubble similar to video placeholder
    const wrap = document.createElement('div');
    wrap.className = 'bubble assistant generating-music';
    wrap.dataset.taskId = taskId;

    const span = document.createElement('span');
    span.className = 'bubble-text';
    span.style.cssText = 'display: flex; flex-direction: column; gap: 4px;';

    // Build info string
    const variationsStr = sampleCount > 1 ? `${sampleCount} variations` : '1 track';

    const textSpan = document.createElement('span');
    textSpan.className = 'generating-message';
    textSpan.innerHTML = `<span class="music-gen-icon">🎵</span> Generating music <span class="music-gen-info">(${variationsStr})</span>`;

    const dots = document.createElement('span');
    dots.className = 'thinking-dots';
    dots.innerHTML = '<span></span><span></span><span></span>';

    // Add estimate text
    const estimate = document.createElement('span');
    estimate.className = 'music-gen-estimate';
    estimate.textContent = 'Lyria-002 • 20-60 seconds';
    estimate.style.cssText = 'font-size: 12px; color: #888;';

    span.appendChild(textSpan);
    span.appendChild(dots);
    span.appendChild(estimate);
    wrap.appendChild(span);

    hist.appendChild(wrap);

    // Scroll to bottom
    hist.scrollTop = hist.scrollHeight;
}

/**
 * Queue video extension
 * @param {string} videoUrl - URL of video to extend
 * @param {string} extensionPrompt - Optional prompt for extension
 * @returns {Promise<string>} Task ID
 */
export async function extendVideoAsync(videoUrl, extensionPrompt = "") {
    try {
        const operator = getOperator();
        const response = await fetch('/extend/video', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                video_url: videoUrl,
                prompt: extensionPrompt,
                operator
            })
        });

        if (!response.ok) {
            const err = await response.json();
            throw new Error(err.detail || `Video extension failed: ${response.statusText}`);
        }

        const data = await response.json();
        taskManager.addTask(data.task_id, 'video_generation', extensionPrompt || 'Extend video');
        toast('Video extension queued (Veo 3.1 - may take 5-20 minutes)...');

        return data.task_id;
    } catch (error) {
        console.error('Video extension error:', error);
        toast(`Video extension failed: ${error.message}`);
        throw error;
    }
}

/**
 * Queue audio analysis
 * @param {string} filePath - Path to audio file
 * @param {string} prompt - Optional analysis prompt
 * @returns {Promise<string>} Task ID
 */
export async function analyzeAudioAsync(filePath, prompt = null) {
    try {
        const operator = getOperator();
        const payload = { file_path: filePath, operator };
        if (prompt) payload.prompt = prompt;

        const response = await fetch('/analyze/audio', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });

        if (!response.ok) {
            const err = await response.json();
            throw new Error(err.detail || `Audio analysis failed: ${response.statusText}`);
        }

        const data = await response.json();
        taskManager.addTask(data.task_id, 'audio_analysis', null);
        toast('Audio analysis queued...');
        return data.task_id;
    } catch (error) {
        console.error('Audio analysis error:', error);
        toast(`Audio analysis failed: ${error.message}`);
        throw error;
    }
}

/**
 * Queue music generation via Lyria-002
 * @param {string} prompt - Music description
 * @param {string} negativePrompt - Optional negative prompt
 * @param {number} seed - Optional seed for reproducibility
 * @param {number} sampleCount - Number of variations to generate (1-4)
 * @returns {Promise<string>} Task ID
 */
export async function generateMusicAsync(prompt, negativePrompt = null, seed = null, sampleCount = 1) {
    const variations = sampleCount > 1 ? ` (${sampleCount} variations)` : '';
    toast(`🎵 Generating music${variations}...`);

    try {
        const body = {
            prompt: prompt,
            operator: getOperator()
        };
        if (negativePrompt) body.negative_prompt = negativePrompt;
        if (seed !== null && !isNaN(seed)) body.seed = seed;
        if (sampleCount > 1) body.sample_count = sampleCount;

        const resp = await fetch('/generate/lyria_music', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });

        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            throw new Error(err.detail || `HTTP ${resp.status}`);
        }

        const data = await resp.json();

        if (data.task_id) {
            const desc = sampleCount > 1
                ? `${prompt.substring(0, 40)}... (${sampleCount} variations)`
                : prompt.substring(0, 50) + '...';
            taskManager.addTask(data.task_id, 'lyria_music', desc);
        }

        const timeEstimate = sampleCount > 1 ? '30-60 seconds' : '20-30 seconds';
        toast(`🎵 Music generation queued (may take ${timeEstimate})...`);
        console.log('[Music] Generation queued:', data);

        return data.task_id;

    } catch (err) {
        console.error('[Music] Generation failed:', err);
        toast(`Music generation failed: ${err.message}`);
        throw err;
    }
}
