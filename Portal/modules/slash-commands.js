/**
 * slash-commands.js
 * Slash command autocomplete dropdown for agent mode
 *
 * Source: Portal/app.js lines 11-334
 * Refactored: 2025-12-20
 */

import { escapeHtml } from './core-utils.js';

// =============================================================================
// Slash Command Dropdown Class
// =============================================================================

/**
 * Dropdown UI for slash command autocomplete in agent mode
 * Supports nested options with tree navigation
 */
export class SlashCommandDropdown {
    constructor() {
        this.dropdown = null;
        this.commands = [];
        this.filteredCommands = [];
        this.selectedIndex = 0;
        this.visible = false;
        this.inputElement = null;
        this.filterText = '';
        // Tree navigation state
        this.level = 0;  // 0 = commands list, 1 = options submenu
        this.expandedCommand = null;  // The command whose options are shown
    }

    /**
     * Fetch available commands from the API
     */
    async fetchCommands() {
        try {
            const response = await fetch('/agent/commands');
            const data = await response.json();
            this.commands = data.commands || [];
            console.log('[SlashCommands] Loaded', this.commands.length, 'commands');
        } catch (err) {
            console.error('[SlashCommands] Failed to fetch commands:', err);
            this.commands = [];
        }
    }

    /**
     * Show the dropdown positioned relative to input element
     * @param {HTMLInputElement} inputElement - The input to position relative to
     */
    show(inputElement) {
        console.log('[SlashCommands] show() called, commands:', this.commands.length);
        if (this.commands.length === 0) {
            console.log('[SlashCommands] No commands to show');
            return;
        }

        this.inputElement = inputElement;
        this.visible = true;
        this.selectedIndex = 0;
        this.filterText = '';
        this.level = 0;
        this.expandedCommand = null;
        this.filteredCommands = [...this.commands];

        if (!this.dropdown) {
            console.log('[SlashCommands] Creating dropdown element');
            this.dropdown = document.createElement('div');
            this.dropdown.className = 'slash-command-dropdown';
            document.body.appendChild(this.dropdown);

            // Use mousedown instead of click to avoid race conditions
            document.addEventListener('mousedown', (e) => {
                if (this.visible && !this.dropdown.contains(e.target) && e.target !== this.inputElement) {
                    console.log('[SlashCommands] Click outside detected, hiding');
                    this.hide();
                }
            });
        }

        this.render();
        this.positionDropdown();
        this.dropdown.classList.remove('hide');
        console.log('[SlashCommands] Dropdown shown, classList:', this.dropdown.className);
    }

    /**
     * Hide the dropdown
     */
    hide() {
        console.log('[SlashCommands] hide() called');
        if (this.dropdown) {
            this.dropdown.classList.add('hide');
        }
        this.visible = false;
        this.filterText = '';
        this.level = 0;
        this.expandedCommand = null;
    }

    /**
     * Navigate back from options to commands list
     */
    goBack() {
        this.level = 0;
        this.expandedCommand = null;
        this.selectedIndex = 0;
        this.render();
    }

    /**
     * Position dropdown above the input element
     */
    positionDropdown() {
        if (!this.inputElement || !this.dropdown) return;
        const rect = this.inputElement.getBoundingClientRect();
        this.dropdown.style.position = 'fixed';
        this.dropdown.style.left = `${rect.left}px`;
        this.dropdown.style.bottom = `${window.innerHeight - rect.top + 5}px`;
        this.dropdown.style.width = `${Math.min(rect.width, 400)}px`;
    }

    /**
     * Filter commands by search text
     * @param {string} text - Filter text
     */
    filter(text) {
        // Only filter when at level 0 (commands list)
        if (this.level !== 0) return;

        this.filterText = text.toLowerCase();
        if (!this.filterText) {
            this.filteredCommands = [...this.commands];
        } else {
            this.filteredCommands = this.commands.filter(cmd =>
                cmd.name.toLowerCase().includes(this.filterText) ||
                (cmd.description && cmd.description.toLowerCase().includes(this.filterText))
            );
        }
        this.selectedIndex = 0;
        this.render();
    }

    /**
     * Render the dropdown content
     */
    render() {
        if (!this.dropdown) return;

        if (this.level === 1 && this.expandedCommand) {
            // Render options submenu
            this.renderOptionsSubmenu();
        } else {
            // Render commands list
            this.renderCommandsList();
        }
    }

    /**
     * Render the main commands list
     */
    renderCommandsList() {
        if (this.filteredCommands.length === 0) {
            this.dropdown.innerHTML = '<div class="slash-command-empty">No matching commands</div>';
            return;
        }

        this.dropdown.innerHTML = this.filteredCommands.map((cmd, idx) => {
            const hasOptions = cmd.options && cmd.options.length > 0;
            const arrowIcon = hasOptions ? '<span class="slash-command-arrow">▶</span>' : '';
            return `
            <div class="slash-command-item ${idx === this.selectedIndex ? 'selected' : ''} ${hasOptions ? 'has-options' : ''}"
                 data-index="${idx}">
                <div class="slash-command-header">
                    <span class="slash-command-name">/${escapeHtml(cmd.name)}</span>
                    ${arrowIcon}
                </div>
                <span class="slash-command-desc">${escapeHtml(cmd.description || '')}</span>
            </div>
        `;
        }).join('');

        this.dropdown.querySelectorAll('.slash-command-item').forEach(item => {
            item.addEventListener('click', () => {
                const idx = parseInt(item.dataset.index);
                this.selectCommand(idx);
            });
        });

        const selected = this.dropdown.querySelector('.selected');
        if (selected) {
            selected.scrollIntoView({ block: 'nearest' });
        }
    }

    /**
     * Render the options submenu for a command
     */
    renderOptionsSubmenu() {
        const cmd = this.expandedCommand;
        const options = cmd.options || [];

        this.dropdown.innerHTML = `
            <div class="slash-command-back" data-action="back">
                <span class="slash-back-arrow">◀</span>
                <span class="slash-back-text">/${escapeHtml(cmd.name)}</span>
            </div>
            <div class="slash-command-submenu-title">${escapeHtml(cmd.description || 'Select an option')}</div>
            ${options.map((opt, idx) => `
                <div class="slash-command-option-item ${idx === this.selectedIndex ? 'selected' : ''}"
                     data-index="${idx}">
                    <span class="slash-option-value">${escapeHtml(opt)}</span>
                </div>
            `).join('')}
        `;

        // Back button handler
        this.dropdown.querySelector('.slash-command-back').addEventListener('click', () => {
            this.goBack();
        });

        // Option click handlers
        this.dropdown.querySelectorAll('.slash-command-option-item').forEach(item => {
            item.addEventListener('click', () => {
                const idx = parseInt(item.dataset.index);
                this.selectOption(idx);
            });
        });

        const selected = this.dropdown.querySelector('.selected');
        if (selected) {
            selected.scrollIntoView({ block: 'nearest' });
        }
    }

    /**
     * Select a command by index
     * @param {number} index - Command index
     */
    selectCommand(index) {
        if (index < 0 || index >= this.filteredCommands.length) return;
        const cmd = this.filteredCommands[index];

        // If command has options, expand to show them
        if (cmd.options && cmd.options.length > 0) {
            this.level = 1;
            this.expandedCommand = cmd;
            this.selectedIndex = 0;
            this.render();
            return;
        }

        // Otherwise, insert the command
        if (this.inputElement && cmd) {
            this.inputElement.value = `/${cmd.name} `;
            this.inputElement.focus();
            this.inputElement.setSelectionRange(
                this.inputElement.value.length,
                this.inputElement.value.length
            );
        }
        this.hide();
    }

    /**
     * Select an option from the submenu
     * @param {number} index - Option index
     */
    selectOption(index) {
        if (!this.expandedCommand) return;
        const options = this.expandedCommand.options || [];
        if (index < 0 || index >= options.length) return;

        const option = options[index];
        const cmdName = this.expandedCommand.name;

        // Insert the full command with option
        if (this.inputElement) {
            this.inputElement.value = `/${cmdName} ${option} `;
            this.inputElement.focus();
            this.inputElement.setSelectionRange(
                this.inputElement.value.length,
                this.inputElement.value.length
            );
        }
        this.hide();
    }

    /**
     * Handle keyboard navigation
     * @param {KeyboardEvent} e - Keyboard event
     * @returns {boolean} Whether the event was handled
     */
    handleKeydown(e) {
        if (!this.visible) return false;

        const itemCount = this.level === 1 && this.expandedCommand
            ? (this.expandedCommand.options || []).length
            : this.filteredCommands.length;

        switch (e.key) {
            case 'ArrowDown':
                e.preventDefault();
                this.selectedIndex = Math.min(this.selectedIndex + 1, itemCount - 1);
                this.render();
                return true;
            case 'ArrowUp':
                e.preventDefault();
                this.selectedIndex = Math.max(this.selectedIndex - 1, 0);
                this.render();
                return true;
            case 'ArrowRight':
                // Expand to options if available
                if (this.level === 0 && this.filteredCommands.length > 0) {
                    const cmd = this.filteredCommands[this.selectedIndex];
                    if (cmd.options && cmd.options.length > 0) {
                        e.preventDefault();
                        this.level = 1;
                        this.expandedCommand = cmd;
                        this.selectedIndex = 0;
                        this.render();
                        return true;
                    }
                }
                return false;
            case 'ArrowLeft':
            case 'Backspace':
                // Go back from options to commands
                if (this.level === 1) {
                    e.preventDefault();
                    this.goBack();
                    return true;
                }
                return false;
            case 'Enter':
            case 'Tab':
                e.preventDefault();
                if (this.level === 1 && this.expandedCommand) {
                    this.selectOption(this.selectedIndex);
                } else if (this.filteredCommands.length > 0) {
                    this.selectCommand(this.selectedIndex);
                }
                return true;
            case 'Escape':
                e.preventDefault();
                if (this.level === 1) {
                    this.goBack();
                } else {
                    this.hide();
                }
                return true;
            default:
                return false;
        }
    }

    /**
     * Handle input changes for filtering
     * @param {Event} e - Input event
     * @param {string} provider - Current provider (only works in 'agents' mode)
     */
    handleInput(e, provider) {
        const value = e.target.value;
        console.log('[SlashCommands] handleInput, visible:', this.visible, 'value:', value, 'provider:', provider);

        // Only work in agents mode
        if (provider !== 'agents') {
            if (this.visible) this.hide();
            return;
        }

        // Show dropdown when value starts with / (regardless of current visibility)
        if (value.startsWith('/')) {
            if (!this.visible) {
                console.log('[SlashCommands] Value starts with /, showing dropdown');
                this.show(e.target);
            }
            const filterText = value.slice(1).split(' ')[0];
            console.log('[SlashCommands] Filtering with:', filterText);
            this.filter(filterText);
        } else if (this.visible) {
            // Only hide if currently visible and value doesn't start with /
            console.log('[SlashCommands] Value does not start with /, hiding');
            this.hide();
        }
    }
}

// =============================================================================
// Module Instance
// =============================================================================

/** Singleton instance of the slash command dropdown */
export let slashCommandDropdown = null;

/**
 * Initialize the slash command dropdown
 * @returns {SlashCommandDropdown} The initialized dropdown
 */
export function initSlashCommandDropdown() {
    if (!slashCommandDropdown) {
        slashCommandDropdown = new SlashCommandDropdown();
    }
    return slashCommandDropdown;
}

/**
 * Get the slash command dropdown instance
 * @returns {SlashCommandDropdown|null} The dropdown instance
 */
export function getSlashCommandDropdown() {
    return slashCommandDropdown;
}
