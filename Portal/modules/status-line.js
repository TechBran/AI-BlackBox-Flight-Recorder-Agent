/**
 * status-line.js
 * Claude Code agent status line for BlackBox Portal
 * Shows context window usage, cost, model, and duration
 *
 * Created: 2026-01-09
 */

import { $ } from './core-utils.js';

// =============================================================================
// StatusLine Class
// =============================================================================

/**
 * StatusLine - Displays Claude Code session status (context/cost/model)
 * Mimics the Claude Code CLI status line at the bottom of the terminal
 */
export class StatusLine {
    constructor() {
        this.container = null;
        this.sessionId = null;
        this.startTime = null;
        this.durationInterval = null;

        // Status data
        this.model = 'Sonnet';
        this.contextUsed = 0;
        this.contextMax = 200000;  // Default context window size
        this.costUsd = 0;
        this.durationMs = 0;
    }

    /**
     * Initialize the status line
     * @param {string} containerId - ID of the status line container
     */
    init(containerId = 'agentStatusLine') {
        this.container = $(containerId);
        this.modelEl = $('agentStatusModel');
        this.contextPercentEl = $('agentContextPercent');
        this.contextFillEl = $('agentContextFill');
        this.costEl = $('agentCost');
        this.durationEl = $('agentDuration');

        // Banner inline metrics elements
        this.bannerModelEl = $('bannerModel');
        this.bannerContextEl = $('bannerContext');
        this.bannerCostEl = $('bannerCost');
        this.bannerDurationEl = $('bannerDuration');

        // Initially hidden
        this.hide();
    }

    /**
     * Show the status line
     */
    show() {
        if (this.container) {
            this.container.classList.remove('hide');
        }
    }

    /**
     * Hide the status line
     */
    hide() {
        if (this.container) {
            this.container.classList.add('hide');
        }
        this.stopDurationTimer();
    }

    /**
     * Start tracking a new session
     * @param {string} sessionId - Session ID
     * @param {string} model - Model name (opus, sonnet, haiku)
     */
    startSession(sessionId, model = 'sonnet') {
        this.sessionId = sessionId;
        this.startTime = Date.now();
        this.costUsd = 0;
        this.contextUsed = 0;

        // Set model display name
        this.setModel(model);

        // Start duration timer
        this.startDurationTimer();

        // Show status line
        this.show();

        // Initial render
        this.render();
    }

    /**
     * Set the model
     * @param {string} model - Model ID (opus, sonnet, haiku)
     */
    setModel(model) {
        const displayNames = {
            'opus': 'Opus 4.7',
            'sonnet': 'Sonnet 4.6',
            'haiku': 'Haiku 4.5',
            'claude-opus-4-8': 'Opus 4.8',
            'claude-opus-4-7': 'Opus 4.7',
            'claude-opus-4-6': 'Opus 4.6',
            'claude-sonnet-4-6': 'Sonnet 4.6',
            'claude-opus-4-5-20251101': 'Opus 4.5',
            'claude-sonnet-4-5-20251022': 'Sonnet 4.5',
            'claude-haiku-4-5-20250120': 'Haiku 4.5'
        };
        this.model = displayNames[model] || model || 'Sonnet';
        this.render();
    }

    /**
     * Update context window usage
     * @param {number} inputTokens - Input tokens used
     * @param {number} outputTokens - Output tokens used
     * @param {number} cacheTokens - Cache tokens (read + creation)
     * @param {number} contextSize - Total context window size
     */
    updateContext(inputTokens, outputTokens, cacheTokens = 0, contextSize = 200000) {
        this.contextUsed = inputTokens + cacheTokens;
        this.contextMax = contextSize;
        this.render();
    }

    /**
     * Update cost
     * @param {number} costUsd - Total cost in USD
     */
    updateCost(costUsd) {
        this.costUsd = costUsd;
        this.render();
    }

    /**
     * Update from a status_update message
     * @param {Object} data - Status update data from backend
     */
    updateFromMessage(data) {
        if (data.model) {
            this.setModel(data.model.id || data.model.display_name || data.model);
        }

        if (data.context_window) {
            const ctx = data.context_window;
            const usage = ctx.current_usage || {};
            const inputTokens = usage.input_tokens || 0;
            const cacheRead = usage.cache_read_input_tokens || 0;
            const cacheCreation = usage.cache_creation_input_tokens || 0;
            this.updateContext(inputTokens, usage.output_tokens || 0, cacheRead + cacheCreation, ctx.context_window_size || 200000);
        }

        if (data.cost) {
            this.updateCost(data.cost.total_cost_usd || 0);
        }

        if (data.duration_ms) {
            this.durationMs = data.duration_ms;
        }
    }

    /**
     * Render the status line
     */
    render() {
        // Context percentage
        const percent = this.contextMax > 0
            ? Math.round((this.contextUsed / this.contextMax) * 100)
            : 0;

        // === Update floating status line ===
        if (this.container) {
            // Model
            if (this.modelEl) {
                this.modelEl.textContent = `[${this.model}]`;
            }

            if (this.contextPercentEl) {
                this.contextPercentEl.textContent = `${percent}%`;
            }

            if (this.contextFillEl) {
                this.contextFillEl.style.width = `${Math.min(percent, 100)}%`;

                // Add warning class if over 80%
                const contextBar = this.contextFillEl.parentElement;
                if (contextBar) {
                    if (percent > 80) {
                        contextBar.classList.add('warning');
                    } else {
                        contextBar.classList.remove('warning');
                    }
                }
            }

            // Cost
            if (this.costEl) {
                this.costEl.textContent = this.costUsd.toFixed(2);
            }
        }

        // === Update banner inline metrics ===
        if (this.bannerModelEl) {
            // Short model name for banner (e.g., "Opus" not "Opus 4.5")
            const shortModel = this.model.split(' ')[0] || this.model;
            this.bannerModelEl.textContent = shortModel;
        }

        if (this.bannerContextEl) {
            this.bannerContextEl.textContent = `${percent}%`;
            // Color based on usage
            if (percent > 80) {
                this.bannerContextEl.style.color = '#ff6b6b';
            } else if (percent > 50) {
                this.bannerContextEl.style.color = '#ffb347';
            } else {
                this.bannerContextEl.style.color = '#27d980';
            }
        }

        if (this.bannerCostEl) {
            this.bannerCostEl.textContent = this.costUsd.toFixed(2);
        }

        // Duration is updated by timer, but render current value
        this.updateDurationDisplay();
    }

    /**
     * Start the duration timer
     */
    startDurationTimer() {
        this.stopDurationTimer();

        this.durationInterval = setInterval(() => {
            this.updateDurationDisplay();
        }, 1000);
    }

    /**
     * Stop the duration timer
     */
    stopDurationTimer() {
        if (this.durationInterval) {
            clearInterval(this.durationInterval);
            this.durationInterval = null;
        }
    }

    /**
     * Update duration display
     */
    updateDurationDisplay() {
        if (!this.startTime) return;

        const elapsed = Date.now() - this.startTime;
        const seconds = Math.floor(elapsed / 1000);

        let durationText;
        if (seconds < 60) {
            durationText = `${seconds}s`;
        } else if (seconds < 3600) {
            const mins = Math.floor(seconds / 60);
            const secs = seconds % 60;
            durationText = `${mins}m ${secs}s`;
        } else {
            const hours = Math.floor(seconds / 3600);
            const mins = Math.floor((seconds % 3600) / 60);
            durationText = `${hours}h ${mins}m`;
        }

        // Update floating status line
        if (this.durationEl) {
            this.durationEl.textContent = durationText;
        }

        // Update banner inline duration
        if (this.bannerDurationEl) {
            this.bannerDurationEl.textContent = durationText;
        }
    }

    /**
     * End the session
     */
    endSession() {
        this.stopDurationTimer();
        // Keep visible for a moment then hide
        setTimeout(() => {
            this.hide();
        }, 5000);
    }

    /**
     * Reset for new session
     */
    reset() {
        this.sessionId = null;
        this.startTime = null;
        this.costUsd = 0;
        this.contextUsed = 0;
        this.durationMs = 0;
        this.stopDurationTimer();
        this.hide();
    }
}

// =============================================================================
// Singleton Instance
// =============================================================================

/** Global status line instance */
export const statusLine = new StatusLine();

// Make available globally
window.statusLine = statusLine;
