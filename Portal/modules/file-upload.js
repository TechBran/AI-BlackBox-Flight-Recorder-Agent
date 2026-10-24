/**
 * file-upload.js
 * File attachment, preview, and upload functionality
 * Handles local file attachments, progress tracking, and native Android file selection
 *
 * Source: Portal/app.js lines 5200-5433
 * Refactored: 2025-12-20
 */

import { $, toast, escapeHtml } from './core-utils.js';
import { scrollToBottomIfNeeded } from './ui-setup.js';

// =============================================================================
// State
// =============================================================================

/** Array of attached files awaiting upload/send */
let attachedFiles = [];

/** Maximum upload size in bytes (must match server MAX_UPLOAD_SIZE) */
const MAX_UPLOAD_SIZE = 500 * 1024 * 1024; // 500MB

// =============================================================================
// Preview Functions
// =============================================================================

/**
 * Render file previews in the preview container
 */
export function renderPreviews() {
    const container = $("previewContainer");
    if (!container) return;
    container.innerHTML = "";

    if (attachedFiles.length === 0) {
        container.classList.add("hide");
        return;
    }

    attachedFiles.forEach((file, index) => {
        const item = document.createElement("div");
        item.className = "preview-item";
        let previewEl;

        if (file.type.startsWith("image/")) {
            previewEl = document.createElement("img");
            previewEl.src = URL.createObjectURL(file);
        } else if (file.type.startsWith("video/")) {
            previewEl = document.createElement("video");
            previewEl.src = URL.createObjectURL(file);
            previewEl.muted = true;
            previewEl.playsInline = true;
            previewEl.play().catch(() => {}); // Attempt to autoplay muted
        } else if (file.type.startsWith("audio/")) {
            previewEl = document.createElement("audio");
            previewEl.src = URL.createObjectURL(file);
            previewEl.controls = true;
        } else if (file.type === "application/pdf" ||
                   file.type === "text/plain" ||
                   file.type === "application/msword" ||
                   file.type === "application/vnd.openxmlformats-officedocument.wordprocessingml.document") {
            // Document preview - show icon and filename
            previewEl = document.createElement("div");
            previewEl.className = "document-preview";
            previewEl.innerHTML = `<span class="doc-icon">📄</span><span class="doc-name">${escapeHtml(file.name)}</span>`;
        }

        if (previewEl) item.appendChild(previewEl);

        const removeBtn = document.createElement("button");
        removeBtn.className = "remove-btn";
        removeBtn.textContent = "✕";
        removeBtn.onclick = () => {
            attachedFiles.splice(index, 1);
            renderPreviews();
        };
        item.appendChild(removeBtn);
        container.appendChild(item);
    });
    container.classList.remove("hide");
}

// =============================================================================
// Upload Functions
// =============================================================================

/**
 * Upload a file to the server with progress tracking
 * @param {File} file - File to upload
 * @returns {Promise<Object>} Upload response with file URL
 */
export async function handleFileUpload(file) {
    // Validate file size before upload
    if (file.size > MAX_UPLOAD_SIZE) {
        const sizeMB = (file.size / (1024 * 1024)).toFixed(0);
        const limitMB = (MAX_UPLOAD_SIZE / (1024 * 1024)).toFixed(0);
        toast(`File too large: ${file.name} (${sizeMB}MB). Maximum is ${limitMB}MB.`);
        return Promise.reject(new Error(`File too large (${sizeMB}MB, max ${limitMB}MB)`));
    }

    // Skip re-upload if Android already uploaded this file directly
    if (file._preUploadedUrl) {
        console.log('[Upload] Skipping re-upload, already uploaded by Android:', file._preUploadedUrl);
        return Promise.resolve({ url: file._preUploadedUrl });
    }

    const formData = new FormData();
    formData.append("file", file);

    return new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();

        // Create progress indicator
        const progressId = `upload-${Date.now()}`;
        const progressContainer = document.createElement('div');
        progressContainer.id = progressId;
        progressContainer.className = 'upload-progress';
        progressContainer.innerHTML = `
            <div class="upload-progress-bar">
                <div class="upload-progress-fill" style="width: 0%"></div>
            </div>
            <span class="upload-progress-text">${escapeHtml(file.name)}: 0%</span>
        `;

        const historyEl = $("history");
        if (historyEl) {
            historyEl.appendChild(progressContainer);
            scrollToBottomIfNeeded();
        }

        // Track upload progress
        xhr.upload.addEventListener('progress', (e) => {
            if (e.lengthComputable) {
                const percentComplete = Math.round((e.loaded / e.total) * 100);
                const fillEl = progressContainer.querySelector('.upload-progress-fill');
                const textEl = progressContainer.querySelector('.upload-progress-text');
                if (fillEl) fillEl.style.width = `${percentComplete}%`;
                if (textEl) textEl.textContent = `${file.name}: ${percentComplete}%`;
            }
        });

        xhr.addEventListener('load', () => {
            // Remove progress indicator
            if (progressContainer.parentNode) {
                progressContainer.parentNode.removeChild(progressContainer);
            }

            if (xhr.status >= 200 && xhr.status < 300) {
                try {
                    const response = JSON.parse(xhr.responseText);
                    resolve(response);
                } catch (e) {
                    reject(new Error('Invalid response from server'));
                }
            } else {
                try {
                    const err = JSON.parse(xhr.responseText);
                    reject(new Error(err.detail || `Upload failed: ${xhr.statusText}`));
                } catch (e) {
                    reject(new Error(`Upload failed: ${xhr.statusText}`));
                }
            }
        });

        xhr.addEventListener('error', () => {
            // Remove progress indicator
            if (progressContainer.parentNode) {
                progressContainer.parentNode.removeChild(progressContainer);
            }
            reject(new Error('Upload failed'));
        });

        xhr.open('POST', '/upload');
        xhr.send(formData);
    });
}

/**
 * Upload file to session-specific folder for Claude Code agent
 * Allows Claude Code to access files directly using file reading tools
 *
 * @param {File} file - The file to upload
 * @param {string} sessionId - The agent session ID
 * @returns {Promise<{path: string, filename: string, session_folder: string, url: string}>}
 */
export async function handleSessionFileUpload(file, sessionId) {
    // Validate file size before upload
    if (file.size > MAX_UPLOAD_SIZE) {
        const sizeMB = (file.size / (1024 * 1024)).toFixed(0);
        const limitMB = (MAX_UPLOAD_SIZE / (1024 * 1024)).toFixed(0);
        toast(`File too large: ${file.name} (${sizeMB}MB). Maximum is ${limitMB}MB.`);
        return Promise.reject(new Error(`File too large (${sizeMB}MB, max ${limitMB}MB)`));
    }

    const formData = new FormData();
    formData.append("file", file);
    formData.append("session_id", sessionId);

    return new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();

        // Create progress indicator
        const progressId = `upload-session-${Date.now()}`;
        const progressContainer = document.createElement('div');
        progressContainer.id = progressId;
        progressContainer.className = 'upload-progress';
        progressContainer.innerHTML = `
            <div class="upload-progress-bar">
                <div class="upload-progress-fill" style="width: 0%"></div>
            </div>
            <span class="upload-progress-text">${escapeHtml(file.name)}: 0%</span>
        `;

        const historyEl = $("history");
        if (historyEl) {
            historyEl.appendChild(progressContainer);
            scrollToBottomIfNeeded();
        }

        // Track upload progress
        xhr.upload.addEventListener('progress', (e) => {
            if (e.lengthComputable) {
                const percentComplete = Math.round((e.loaded / e.total) * 100);
                const fillEl = progressContainer.querySelector('.upload-progress-fill');
                const textEl = progressContainer.querySelector('.upload-progress-text');
                if (fillEl) fillEl.style.width = `${percentComplete}%`;
                if (textEl) textEl.textContent = `${file.name}: ${percentComplete}%`;
            }
        });

        xhr.addEventListener('load', () => {
            // Remove progress indicator
            if (progressContainer.parentNode) {
                progressContainer.parentNode.removeChild(progressContainer);
            }

            if (xhr.status >= 200 && xhr.status < 300) {
                try {
                    const response = JSON.parse(xhr.responseText);
                    console.log('[Session Upload] Success:', response);
                    resolve(response);
                } catch (e) {
                    reject(new Error('Invalid response from server'));
                }
            } else {
                try {
                    const err = JSON.parse(xhr.responseText);
                    reject(new Error(err.detail || `Upload failed: ${xhr.statusText}`));
                } catch (e) {
                    reject(new Error(`Upload failed: ${xhr.statusText}`));
                }
            }
        });

        xhr.addEventListener('error', () => {
            if (progressContainer.parentNode) {
                progressContainer.parentNode.removeChild(progressContainer);
            }
            reject(new Error('Upload failed'));
        });

        xhr.open('POST', '/upload/session');
        xhr.send(formData);
    });
}

// =============================================================================
// Native Android File Selection
// =============================================================================

/**
 * Open the Android file picker for a chat attachment. A reference / start
 * image pick the operator cancelled leaves its selection flag set (Android
 * calls no JS on a cancel), so generation-modals.js would hand this pick to
 * that modal; the composer claims the picker back first.
 * @param {string} acceptTypes - The picker's MIME accept list
 */
export function openNativeAttachPicker(acceptTypes) {
    window.referenceImageSelectionActive = false;
    window.videoStartImageSelectionActive = false;
    AndroidFilePicker.openFileChooser(acceptTypes);
}

/**
 * Setup native Android file selection callback
 */
export function setupNativeFileCallback() {
    // Handler for large files uploaded directly from Android via OkHttp
    // Receives the server URL instead of Base64 data (no memory explosion).
    // Picks for a generation modal's reference / start image are routed there
    // by generation-modals.js, which wraps this handler.
    window.onNativeFileUploaded = function(uploadUrl, mimeType, fileName) {
        try {
            // Create a File object from the uploaded URL for normal attachment flow
            fetch(uploadUrl)
                .then(r => r.blob())
                .then(blob => {
                    const file = new File([blob], fileName, { type: mimeType });
                    // Store the pre-uploaded URL so we don't re-upload
                    file._preUploadedUrl = uploadUrl;
                    attachedFiles.push(file);
                    renderPreviews();
                    console.log('[NativeFileUploaded] File attached from direct upload:', fileName, uploadUrl);
                    toast(`Attached ${fileName}`);
                })
                .catch(e => {
                    console.error('[NativeFileUploaded] Error creating file from upload:', e);
                    toast('Failed to attach uploaded file.');
                });
        } catch (e) {
            console.error('[NativeFileUploaded] Error:', e);
            toast('Failed to process uploaded file.');
        }
    };

    window.onNativeFileSelected = function(base64Data, mimeType, fileName) {
        try {
            // Normal attachment handling (generation-modals.js wraps this
            // handler to route reference / start image picks to its modals)
            const byteCharacters = atob(base64Data);
            const byteNumbers = new Array(byteCharacters.length);
            for (let i = 0; i < byteCharacters.length; i++) {
                byteNumbers[i] = byteCharacters.charCodeAt(i);
            }
            const byteArray = new Uint8Array(byteNumbers);
            const blob = new Blob([byteArray], { type: mimeType });
            const file = new File([blob], fileName, { type: mimeType });

            attachedFiles.push(file);
            renderPreviews();
            console.log('[NativeFile] File attached, attachedFiles now:', attachedFiles.length, attachedFiles.map(f => f.name));
            toast(`Attached ${fileName} (${attachedFiles.length} total)`);
        } catch (e) {
            console.error("Error processing native file:", e);
            toast("Failed to attach file from device.");
        }
    };
}

// =============================================================================
// State Access
// =============================================================================

/**
 * Get attached files array
 * @returns {File[]} Attached files
 */
export function getAttachedFiles() {
    return attachedFiles;
}

/**
 * Clear all attached files
 */
export function clearAttachedFiles() {
    attachedFiles = [];
    renderPreviews();
}

/**
 * Add a file to attachments
 * @param {File} file - File to add
 */
export function addAttachedFile(file) {
    if (file.size > MAX_UPLOAD_SIZE) {
        const sizeMB = (file.size / (1024 * 1024)).toFixed(0);
        const limitMB = (MAX_UPLOAD_SIZE / (1024 * 1024)).toFixed(0);
        toast(`File too large: ${file.name} (${sizeMB}MB). Maximum is ${limitMB}MB.`);
        return false;
    }
    attachedFiles.push(file);
    renderPreviews();
    return true;
}

/**
 * Remove a file from attachments by index
 * @param {number} index - Index to remove
 */
export function removeAttachedFile(index) {
    attachedFiles.splice(index, 1);
    renderPreviews();
}

/**
 * Check if there are any attached files
 * @returns {boolean} Has attachments
 */
export function hasAttachments() {
    return attachedFiles.length > 0;
}
