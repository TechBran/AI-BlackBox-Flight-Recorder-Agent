/**
 * todo-tracker.js
 * Claude Code TodoWrite integration for BlackBox Portal
 * Provides task tracking UI similar to Claude Code CLI
 *
 * Created: 2026-01-09
 */

import { $, escapeHtml as sharedEscapeHtml, escapeAttr } from './core-utils.js';

// =============================================================================
// TodoTracker Class
// =============================================================================

/**
 * TodoTracker - Manages and displays Claude Code's TodoWrite tasks
 * Provides real-time task tracking with auto-collapse on completion
 */
export class TodoTracker {
    constructor() {
        this.todos = [];
        this.sessionId = null;
        this.container = null;
        this.isCollapsed = false;
        this.allCompleted = false;
    }

    /**
     * Initialize the tracker with a container element
     * @param {string} containerId - ID of the container element
     * @param {string} sessionId - Current agent session ID
     */
    init(containerId, sessionId) {
        this.container = $(containerId);
        this.sessionId = sessionId;
        this.load();
        this.render();
    }

    /**
     * Update todos from WebSocket event
     * @param {Array} todosArray - Array of todo objects from TodoWrite
     */
    updateTodos(todosArray) {
        if (!Array.isArray(todosArray)) {
            console.warn('[TodoTracker] Invalid todos array:', todosArray);
            return;
        }

        this.todos = todosArray;
        this.checkCompletion();
        this.save();
        this.render();
    }

    /**
     * Check if all todos are completed and auto-collapse
     */
    checkCompletion() {
        if (this.todos.length === 0) {
            this.allCompleted = false;
            return;
        }

        const completed = this.todos.filter(t => t.status === 'completed').length;
        this.allCompleted = completed === this.todos.length;

        // Auto-collapse when all complete
        if (this.allCompleted && !this.isCollapsed) {
            this.isCollapsed = true;
        }
    }

    /**
     * Get progress statistics
     * @returns {Object} Progress stats {completed, inProgress, pending, total, percent}
     */
    getProgress() {
        const completed = this.todos.filter(t => t.status === 'completed').length;
        const inProgress = this.todos.filter(t => t.status === 'in_progress').length;
        const pending = this.todos.filter(t => t.status === 'pending').length;
        const total = this.todos.length;
        const percent = total > 0 ? Math.round((completed / total) * 100) : 0;

        return { completed, inProgress, pending, total, percent };
    }

    /**
     * Get the currently active task (in_progress)
     * @returns {Object|null} The in_progress todo or null
     */
    getCurrentTask() {
        return this.todos.find(t => t.status === 'in_progress') || null;
    }

    /**
     * Toggle collapsed state
     */
    toggleCollapse() {
        this.isCollapsed = !this.isCollapsed;
        this.render();
    }

    /**
     * Render the todo panel
     */
    render() {
        if (!this.container) return;

        // Hide if no todos
        if (this.todos.length === 0) {
            this.container.classList.add('hide');
            this.container.innerHTML = '';
            return;
        }

        this.container.classList.remove('hide');

        const progress = this.getProgress();
        const currentTask = this.getCurrentTask();

        // Build HTML
        let html = '';

        if (this.isCollapsed) {
            // Collapsed view - single line summary
            html = `
                <div class="todo-collapsed">
                    <span class="todo-summary-icon">${this.allCompleted ? '✅' : '📋'}</span>
                    <span class="todo-summary-text">${progress.completed}/${progress.total} completed</span>
                    ${currentTask ? `<span class="todo-current-mini">• ${this.escapeHtml(currentTask.activeForm || currentTask.content)}</span>` : ''}
                    <span class="todo-expand-btn">▼</span>
                </div>
            `;
        } else {
            // Expanded view - full task list
            html = `
                <div class="todo-header">
                    <span class="todo-title">📋 Tasks</span>
                    <span class="todo-progress">${progress.completed}/${progress.total}</span>
                    <div class="todo-progress-bar">
                        <div class="todo-progress-fill" style="width: ${progress.percent}%"></div>
                    </div>
                    <span class="todo-collapse-btn">▲</span>
                </div>
            `;

            // Show current task prominently if exists
            if (currentTask) {
                html += `
                    <div class="todo-current">
                        <span class="todo-spinner"></span>
                        <span class="todo-active-form">${this.escapeHtml(currentTask.activeForm || currentTask.content)}</span>
                    </div>
                `;
            }

            // Task list
            html += '<div class="todo-list">';
            for (const todo of this.todos) {
                const statusClass = todo.status;
                const icon = this.getStatusIcon(todo.status);
                const content = todo.status === 'in_progress' && todo.activeForm
                    ? todo.activeForm
                    : todo.content;

                html += `
                    <div class="todo-item ${escapeAttr(statusClass)}">
                        <span class="todo-check">${icon}</span>
                        <span class="todo-content">${this.escapeHtml(content)}</span>
                    </div>
                `;
            }
            html += '</div>';
        }

        this.container.innerHTML = html;

        // Toggle via a listener (no inline onclick string in the markup)
        const toggle = this.container.querySelector('.todo-collapsed, .todo-header');
        if (toggle) toggle.addEventListener('click', () => this.toggleCollapse());
    }

    /**
     * Get status icon for todo state
     * @param {string} status - Todo status
     * @returns {string} Icon string
     */
    getStatusIcon(status) {
        switch (status) {
            case 'completed': return '✓';
            case 'in_progress': return '<span class="todo-spinner-inline"></span>';
            case 'pending': return '○';
            default: return '•';
        }
    }

    /**
     * Save todos to SessionStorage
     */
    save() {
        if (!this.sessionId) return;

        const data = {
            todos: this.todos,
            isCollapsed: this.isCollapsed,
            allCompleted: this.allCompleted
        };

        try {
            sessionStorage.setItem(`bb_todos_${this.sessionId}`, JSON.stringify(data));
        } catch (e) {
            console.error('[TodoTracker] Failed to save:', e);
        }
    }

    /**
     * Load todos from SessionStorage
     */
    load() {
        if (!this.sessionId) return;

        try {
            const stored = sessionStorage.getItem(`bb_todos_${this.sessionId}`);
            if (stored) {
                const data = JSON.parse(stored);
                this.todos = data.todos || [];
                this.isCollapsed = data.isCollapsed || false;
                this.allCompleted = data.allCompleted || false;
            }
        } catch (e) {
            console.error('[TodoTracker] Failed to load:', e);
        }
    }

    /**
     * Clear todos (for new session)
     */
    clear() {
        this.todos = [];
        this.isCollapsed = false;
        this.allCompleted = false;
        if (this.sessionId) {
            sessionStorage.removeItem(`bb_todos_${this.sessionId}`);
        }
        this.render();
    }

    /**
     * Set new session ID
     * @param {string} sessionId - New session ID
     */
    setSession(sessionId) {
        this.sessionId = sessionId;
        this.load();
        this.render();
    }

    /**
     * Escape HTML characters (shared core-utils helper; also escapes quotes)
     * @param {string} text - Text to escape
     * @returns {string} Escaped text
     */
    escapeHtml(text) {
        return sharedEscapeHtml(text);
    }
}

// =============================================================================
// Singleton Instance
// =============================================================================

/** Global todo tracker instance */
export const todoTracker = new TodoTracker();

// Make available globally (console / legacy callers; the panel itself now binds listeners)
window.todoTracker = todoTracker;
