// web/static/js/main.js
console.log('✅ main.js загружен');

class DieselMonitoring {
    constructor() {
        this.controllers = [];
        this.currentControllerId = '';
        this.currentDate = this.getTodayDate();
        this.currentTab = 'events';
        this.chart = null;
        this.exportType = null;
        this.chartHours = 24;
        this.statusUpdateInterval = null;
        this.namesCache = {};
        this.colors = ['#002f87', '#ec6d22', '#10b981', '#8b5cf6', '#ef4444', '#06b6d4', '#f59e0b', '#ec4899'];
        this.init();
    }

    getTodayDate() {
        const today = new Date();
        const mskDate = new Date(today.toLocaleString('en-US', { timeZone: 'Europe/Moscow' }));
        return mskDate.toISOString().split('T')[0];
    }

    getMskTime() {
        const now = new Date();
        return new Date(now.toLocaleString('en-US', { timeZone: 'Europe/Moscow' }));
    }

    formatMskTime(timestamp) {
        const date = new Date(timestamp * 1000);
        const mskDate = new Date(date.toLocaleString('en-US', { timeZone: 'Europe/Moscow' }));
        return mskDate.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    }

    formatDateTimeForTooltip(timestamp, hours) {
        const date = new Date(timestamp * 1000);
        const mskDate = new Date(date.toLocaleString('en-US', { timeZone: 'Europe/Moscow' }));

        if (hours > 24) {
            return mskDate.toLocaleString('ru-RU', {
                day: '2-digit',
                month: '2-digit',
                year: 'numeric',
                hour: '2-digit',
                minute: '2-digit'
            });
        } else {
            return mskDate.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
        }
    }

    formatXAxisLabel(timestamp, hours) {
        const date = new Date(timestamp * 1000);
        const mskDate = new Date(date.toLocaleString('en-US', { timeZone: 'Europe/Moscow' }));

        if (hours > 48) {
            return mskDate.toLocaleDateString('ru-RU', { day: 'numeric', month: 'numeric' });
        } else if (hours > 24) {
            return mskDate.toLocaleString('ru-RU', { day: 'numeric', month: 'numeric', hour: '2-digit', minute: '2-digit' });
        } else {
            return mskDate.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
        }
    }

    async init() {
        console.log('🚀 Инициализация начата');

        await this.loadControllers();
        this.setupEventListeners();
        this.loadNotifications();
        this.loadParameterChart();
        this.loadCurrentAlerts();
        this.updateClock();
        this.adjustNotificationsHeight();

        await this.initLLM();
        this.initDropdowns();
        
        setInterval(() => this.loadCurrentAlerts(), 5000);
        setInterval(() => {
            if (this.currentTab === 'status') {
                this.loadCurrentStatus();
            }
        }, 10000);
        setInterval(() => {
            if (this.currentTab === 'events') {
                this.loadNotifications();
                this.adjustNotificationsHeight();
            }
        }, 30000);
        setInterval(() => {
            this.loadParameterChart();
        }, 10000);
        setInterval(() => this.updateClock(), 1000);

        window.addEventListener('resize', () => this.adjustNotificationsHeight());
    }

    adjustNotificationsHeight() {
        const eventsContainer = document.getElementById('events-container');
        const notificationsContainer = document.getElementById('notifications-container');
        const statusContainer = document.getElementById('status-container');
        const eventsFilter = document.getElementById('events-filter');

        if (!eventsContainer || !notificationsContainer) return;

        let availableHeight = window.innerHeight - 250;

        if (eventsFilter && this.currentTab === 'events') {
            availableHeight -= eventsFilter.offsetHeight;
        }

        availableHeight = Math.max(1000, Math.min(availableHeight, 1000));

        notificationsContainer.style.height = `${availableHeight}px`;

        if (statusContainer) {
            statusContainer.style.height = `${availableHeight}px`;
            const statusContent = document.getElementById('status-content');
            if (statusContent) {
                statusContent.style.height = `${availableHeight}px`;
            }
        }
    }

    async loadControllers() {
        try {
            const response = await fetch('/api/controllers');
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            this.controllers = await response.json();
            console.log('✅ Контроллеры загружены:', this.controllers);

            const activeCounter = document.getElementById('active-units-count');
            if (activeCounter) activeCounter.textContent = this.controllers.length;

            const select = document.getElementById('controller-filter');
            if (select) {
                select.innerHTML = '<option value="">Все установки</option>';

                for (const ctrl of this.controllers) {
                    this.namesCache[ctrl.id] = await this.getControllerDisplayName(ctrl.id);
                }

                for (const ctrl of this.controllers) {
                    const option = document.createElement('option');
                    option.value = ctrl.id;
                    option.textContent = this.namesCache[ctrl.id];
                    select.appendChild(option);
                }

                if (this.controllers.length > 0 && !this.currentControllerId) {
                    this.currentControllerId = this.controllers[0].id.toString();
                    select.value = this.currentControllerId;
                }
            }
        } catch (error) {
            console.error('❌ Ошибка загрузки контроллеров:', error);
        }
    }

    async getControllerDisplayName(controllerId) {
        if (this.namesCache[controllerId]) {
            return this.namesCache[controllerId];
        }

        try {
            const response = await fetch(`/api/controller-name/${controllerId}`);
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            const data = await response.json();
            const displayName = data.ip ? `${data.name} (${data.ip})` : data.name;
            this.namesCache[controllerId] = displayName;
            return displayName;
        } catch (error) {
            console.error('Ошибка получения имени контроллера:', error);
            const controller = this.controllers.find(c => c.id == controllerId);
            const fallbackName = controller ? controller.name : `ДГУ ${controllerId}`;
            const displayName = controller && controller.ip ? `${fallbackName} (${controller.ip})` : fallbackName;
            this.namesCache[controllerId] = displayName;
            return displayName;
        }
    }

    async loadBitsStatus(controllerId) {
        try {
            const response = await fetch(`/api/current-bits-status?controller_id=${controllerId}`);
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            const data = await response.json();
            return data.bits || {};
        } catch (error) {
            console.error('Ошибка загрузки статуса битов:', error);
            return {};
        }
    }

    async loadCurrentAlerts() {
        try {
            let url = '/api/current-alerts';
            if (this.currentControllerId && this.currentControllerId !== '') {
                url += `?controller_id=${this.currentControllerId}`;
            }

            const response = await fetch(url);
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            const alerts = await response.json();

            const alertsContainer = document.getElementById('current-alerts');
            const alertsSummary = document.getElementById('alerts-summary');
            if (!alertsContainer) return;

            const activeProblems = alerts.filter(a => a.severity === 'alarm' || a.severity === 'warn');
            const totalAlarms = activeProblems.filter(a => a.severity === 'alarm').length;
            const totalWarns = activeProblems.filter(a => a.severity === 'warn').length;

            if (alertsSummary) {
                alertsSummary.innerHTML = `
                    ${totalAlarms > 0 ? `<span class="text-xs bg-red-100 text-red-700 px-2 py-1 rounded-full font-semibold">🚨 ${totalAlarms}</span>` : ''}
                    ${totalWarns > 0 ? `<span class="text-xs bg-yellow-100 text-yellow-700 px-2 py-1 rounded-full font-semibold">⚠️ ${totalWarns}</span>` : ''}
                    ${totalAlarms === 0 && totalWarns === 0 ? '<span class="text-xs bg-green-100 text-green-700 px-2 py-1 rounded-full">✅</span>' : ''}
                `;
            }

            if (activeProblems.length === 0) {
                alertsContainer.innerHTML = `
                    <div class="text-center text-green-600 py-4 text-sm flex items-center justify-center gap-2">
                        <span>✅</span> Все параметры в норме, активных аварий нет
                    </div>
                `;
                return;
            }

            const grouped = {};
            for (const alert of activeProblems) {
                const controllerName = await this.getControllerDisplayName(alert.controller_id);
                if (!grouped[controllerName]) grouped[controllerName] = [];
                grouped[controllerName].push(alert);
            }

            let html = '';
            for (const [controllerName, items] of Object.entries(grouped)) {
                const alarms = items.filter(a => a.severity === 'alarm');
                const warns = items.filter(a => a.severity === 'warn');

                html += `
                    <div class="mb-4 border-b border-gray-200 pb-3 last:border-0">
                        <div class="flex justify-between items-center mb-3">
                            <div class="flex items-center gap-2">
                                <span class="text-xs text-gray-500">📌</span>
                                <span class="text-sm font-medium text-gray-600">${this.escapeHtml(controllerName)}</span>
                            </div>
                            <div class="flex gap-2">
                                ${alarms.length > 0 ? `<span class="text-xs bg-red-100 text-red-700 px-2 py-1 rounded-full font-semibold">🚨 ${alarms.length}</span>` : ''}
                                ${warns.length > 0 ? `<span class="text-xs bg-yellow-100 text-yellow-700 px-2 py-1 rounded-full font-semibold">⚠️ ${warns.length}</span>` : ''}
                            </div>
                        </div>
                `;

                items.forEach(alert => {
                    const color = alert.severity === 'alarm' ? 'red' : 'yellow';
                    const icon = alert.severity === 'alarm' ? '🚨' : '⚠️';

                    html += `
                        <div class="bg-${color}-50 border-l-4 border-${color}-500 rounded-lg p-2 mb-2 text-xs">
                            <div class="font-medium text-${color}-800">${icon} ${this.escapeHtml(alert.title)}</div>
                            <div class="text-${color}-600 mt-0.5">${this.escapeHtml(alert.details)}</div>
                            <div class="text-${color}-400 font-mono mt-0.5">${alert.code}</div>
                        </div>
                    `;
                });

                html += `</div>`;
            }

            alertsContainer.innerHTML = html;

        } catch (error) {
            console.error('Ошибка загрузки текущих аварий:', error);
            const alertsContainer = document.getElementById('current-alerts');
            if (alertsContainer) {
                alertsContainer.innerHTML = '<div class="text-center text-red-500 py-4 text-sm">❌ Ошибка загрузки</div>';
            }
        }
    }

    async loadNotifications() {
        try {
            let url = `/api/notifications?limit=150&date=${this.currentDate}`;
            if (this.currentControllerId && this.currentControllerId !== '') {
                url += `&controller_id=${this.currentControllerId}`;
            }

            const response = await fetch(url);
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            const notifications = await response.json();

            const container = document.getElementById('notifications-container');
            if (!container) return;

            if (notifications.length === 0) {
                container.innerHTML = '<div class="text-center text-gray-400 py-8">Нет событий за выбранную дату</div>';
                return;
            }

            container.innerHTML = '';

            for (const notif of notifications) {
                let bgColor, borderColor, icon;

                switch(notif.severity) {
                    case 'alarm':
                        bgColor = 'bg-red-50';
                        borderColor = 'border-red-500';
                        icon = '🚨';
                        break;
                    case 'warn':
                        bgColor = 'bg-yellow-50';
                        borderColor = 'border-yellow-500';
                        icon = '⚠️';
                        break;
                    case 'success':
                        bgColor = 'bg-green-50';
                        borderColor = 'border-green-500';
                        icon = '✅';
                        break;
                    default:
                        bgColor = 'bg-gray-50';
                        borderColor = 'border-gray-300';
                        icon = 'ℹ️';
                }

                const controllerDisplay = await this.getControllerDisplayName(notif.controller_id);

                let timeStr = notif.time_msk || '';
                if (notif.ts) {
                    timeStr = this.formatMskTime(notif.ts);
                }

                let severityText = notif.severity || 'info';
                if (severityText === 'success') severityText = 'успех';
                else if (severityText === 'alarm') severityText = 'авария';
                else if (severityText === 'warn') severityText = 'предупреждение';
                else severityText = 'инфо';

                container.innerHTML += `
                    <div class="${bgColor} border-l-4 ${borderColor} rounded-lg p-3 mb-2 transition-all hover:shadow-md">
                        <div class="flex justify-between items-start">
                            <span class="text-xs text-gray-500">${timeStr}</span>
                            <span class="text-xs px-2 py-0.5 rounded-full ${this.getSeverityBadgeClass(notif.severity)}">${icon} ${severityText}</span>
                        </div>
                        <div class="text-xs text-gray-500 mt-0.5">📌 ${this.escapeHtml(controllerDisplay)}</div>
                        <div class="mt-1.5 text-sm font-medium ${this.getTitleClass(notif.severity)}">${this.escapeHtml(notif.clean_title || notif.title)}</div>
                        <div class="text-xs ${this.getDetailsClass(notif.severity)} mt-0.5" style="white-space: pre-line;">${this.escapeHtml(notif.clean_details || notif.details || '')}</div>
                        <div class="text-xs text-gray-400 mt-1 font-mono">${this.escapeHtml(notif.code || '')}</div>
                    </div>
                `;
            }

            const counter = document.getElementById('total-notifications-count');
            if (counter) counter.textContent = notifications.length;

            this.adjustNotificationsHeight();

        } catch (error) {
            console.error('❌ Ошибка уведомлений:', error);
            const container = document.getElementById('notifications-container');
            if (container) {
                container.innerHTML = '<div class="text-center text-red-500 py-8">❌ Ошибка загрузки уведомлений</div>';
            }
        }
    }

    getSeverityBadgeClass(severity) {
        switch(severity) {
            case 'alarm': return 'bg-red-100 text-red-700';
            case 'warn': return 'bg-yellow-100 text-yellow-700';
            case 'success': return 'bg-green-100 text-green-700';
            default: return 'bg-gray-100 text-gray-600';
        }
    }

    getTitleClass(severity) {
        switch(severity) {
            case 'alarm': return 'text-red-800';
            case 'warn': return 'text-yellow-800';
            case 'success': return 'text-green-800';
            default: return 'text-gray-800';
        }
    }

    getDetailsClass(severity) {
        switch(severity) {
            case 'alarm': return 'text-red-600';
            case 'warn': return 'text-yellow-600';
            case 'success': return 'text-green-600';
            default: return 'text-gray-600';
        }
    }

    async loadCurrentStatus() {
        let controllerId = this.currentControllerId;
        if (!controllerId && this.controllers.length > 0) {
            controllerId = this.controllers[0].id;
        }

        if (!controllerId) {
            const statusContent = document.getElementById('status-content');
            if (statusContent) {
                statusContent.innerHTML = '<div class="text-center text-gray-400 py-8">Нет доступных установок</div>';
            }
            return;
        }

        try {
            const response = await fetch(`/api/status/${controllerId}`);
            if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);
            const data = await response.json();

            const statusContent = document.getElementById('status-content');
            const mskNow = this.getMskTime();
            const updateTime = mskNow.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit', second: '2-digit' });

            if (data.status_data) {
                const html = await this.renderPrettyStatus(data.status_data, updateTime, controllerId);
                statusContent.innerHTML = html;
            }

        } catch (error) {
            console.error('Ошибка загрузки статуса:', error);
            const statusContent = document.getElementById('status-content');
            if (statusContent) {
                statusContent.innerHTML = '<div class="text-center text-red-500 py-8">Ошибка загрузки статуса</div>';
            }
        }
    }

    async renderPrettyStatus(data, updateTime, controllerId) {
        const s = data;
        const displayName = this.namesCache[controllerId] || s.unit_name || 'ДГУ';

        const formatValue = (value, unit) => {
            if (value === '—') return '—';
            return `${value} ${unit}`;
        };

        const bitsStatus = await this.loadBitsStatus(controllerId);

        let bitsHtml = '';
        if (Object.keys(bitsStatus).length > 0) {
            bitsHtml = `
                <div class="bg-white rounded-lg p-3 shadow-sm border border-gray-100 mt-4">
                    <h3 class="text-sm font-semibold text-diesel-blue mb-3 flex items-center gap-1">
                        <span class="text-lg">🔔</span> Важные сигналы
                    </h3>
                    <div class="grid grid-cols-2 md:grid-cols-3 gap-2">
            `;

            for (const [addr, bit] of Object.entries(bitsStatus)) {
                const isActive = bit.state === 'ВКЛ';
                const bgClass = isActive ? 'bg-red-50' : 'bg-green-50';
                const textClass = isActive ? 'text-red-700' : 'text-green-700';
                const stateIcon = isActive ? '🔴' : '🟢';

                bitsHtml += `
                    <div class="${bgClass} rounded-lg p-2">
                        <div class="flex items-start justify-between gap-2">
                            <span class="text-xs font-medium ${textClass} flex-1 break-words">${this.escapeHtml(bit.name)}</span>
                            <div class="flex items-center gap-1 flex-shrink-0 mt-0.5">
                                <span class="text-sm">${stateIcon}</span>
                                <span class="text-xs font-mono font-bold ${textClass}">${bit.state}</span>
                            </div>
                        </div>
                    </div>
                `;
            }

            bitsHtml += `
                    </div>
                </div>
            `;
        }

        return `
            <div class="space-y-4">
                <div class="flex justify-between items-center text-xs text-gray-500 pb-2 border-b border-gray-200">
                    <div class="flex items-center gap-2">
                        <span class="w-2 h-2 bg-green-500 rounded-full animate-pulse"></span>
                        <span>🔄 Обновлено: ${updateTime} МСК</span>
                    </div>
                    <div class="font-medium bg-diesel-blue/10 text-diesel-blue px-2 py-1 rounded">
                        ${this.escapeHtml(displayName)}
                    </div>
                </div>

                <div class="grid grid-cols-4 gap-2">
                    <div class="bg-gradient-to-br from-blue-50 to-blue-100/50 rounded-lg p-3 text-center">
                        <div class="text-xs text-blue-600 mb-1">Двигатель</div>
                        <div class="font-bold text-blue-800">${s.general.engine_state}</div>
                    </div>
                    <div class="bg-gradient-to-br from-purple-50 to-purple-100/50 rounded-lg p-3 text-center">
                        <div class="text-xs text-purple-600 mb-1">Режим</div>
                        <div class="font-bold text-purple-800">${s.general.mode}</div>
                    </div>
                    <div class="bg-gradient-to-br ${s.general.mains_ok === 1 ? 'from-green-50 to-green-100/50' : 'from-red-50 to-red-100/50'} rounded-lg p-3 text-center">
                        <div class="text-xs ${s.general.mains_ok === 1 ? 'text-green-600' : 'text-red-600'} mb-1">Сеть</div>
                        <div class="font-bold ${s.general.mains_ok === 1 ? 'text-green-800' : 'text-red-800'}">${s.general.mains_state.replace('✅', '').replace('🚨', '')}</div>
                    </div>
                    <div class="bg-gradient-to-br from-gray-50 to-gray-100/50 rounded-lg p-3 text-center">
                        <div class="text-xs text-gray-600 mb-1">Контакторы</div>
                        <div class="font-bold text-gray-800">ABC: ${s.general.contacts_avs} | ABГ: ${s.general.contacts_avg}</div>
                    </div>
                </div>

                <div class="grid grid-cols-2 gap-4">
                    <div class="space-y-4">
                        <div class="bg-white rounded-lg p-3 shadow-sm border border-gray-100">
                            <h3 class="text-sm font-semibold text-diesel-blue mb-3 flex items-center gap-1">
                                <span class="text-lg">🔧</span> Двигатель
                            </h3>
                            <div class="space-y-2">
                                <div class="flex justify-between items-center py-1 border-b border-gray-50">
                                    <span class="text-xs text-gray-500">Обороты (RPM)</span>
                                    <span class="font-mono font-medium text-diesel-blue">${formatValue(s.engine.rpm, 'об/мин')}</span>
                                </div>
                                <div class="flex justify-between items-center py-1 border-b border-gray-50">
                                    <span class="text-xs text-gray-500">Нагрузка (P)</span>
                                    <span class="font-mono font-medium text-diesel-blue">${formatValue(s.engine.p_load, 'кВт')}</span>
                                </div>
                                <div class="flex justify-between items-center py-1">
                                    <span class="text-xs text-gray-500">Температура ОЖ</span>
                                    <span class="font-mono font-medium text-orange-600">${formatValue(s.sensors.temp, '°C')}</span>
                                </div>
                            </div>
                        </div>

                        <div class="bg-white rounded-lg p-3 shadow-sm border border-gray-100">
                            <h3 class="text-sm font-semibold text-diesel-blue mb-3 flex items-center gap-1">
                                <span class="text-lg">🧰</span> Датчики
                            </h3>
                            <div class="grid grid-cols-2 gap-2">
                                <div class="bg-gray-50 p-2 rounded">
                                    <div class="text-xs text-gray-500">АКБ</div>
                                    <div class="font-mono font-medium text-diesel-blue">${formatValue(s.sensors.battery, 'В')}</div>
                                </div>
                                <div class="bg-gray-50 p-2 rounded">
                                    <div class="text-xs text-gray-500">Масло</div>
                                    <div class="font-mono font-medium text-diesel-blue">${formatValue(s.sensors.oil, 'Бар')}</div>
                                </div>
                                <div class="bg-gray-50 p-2 rounded">
                                    <div class="text-xs text-gray-500">Топливо</div>
                                    <div class="font-mono font-medium ${parseFloat(s.sensors.fuel) < 20 ? 'text-red-600' : 'text-diesel-blue'}">${formatValue(s.sensors.fuel, '%')}</div>
                                </div>
                            </div>
                        </div>
                    </div>

                    <div class="space-y-4">
                        <div class="bg-white rounded-lg p-3 shadow-sm border border-gray-100">
                            <h3 class="text-sm font-semibold text-diesel-blue mb-3 flex items-center gap-1">
                                <span class="text-lg">⚡️</span> Генератор
                            </h3>
                            <div class="space-y-2">
                                <div class="flex justify-between items-center py-1 border-b border-gray-50">
                                    <span class="text-xs text-gray-500">Частота</span>
                                    <span class="font-mono font-medium">${formatValue(s.generator.f_gen, 'Гц')}</span>
                                </div>
                                <div class="grid grid-cols-3 gap-1 mt-2">
                                    <div class="text-center">
                                        <div class="text-xs text-gray-400">L1</div>
                                        <div class="font-mono text-sm">${formatValue(s.generator.u_gen_l1, 'В')}</div>
                                    </div>
                                    <div class="text-center">
                                        <div class="text-xs text-gray-400">L2</div>
                                        <div class="font-mono text-sm">${formatValue(s.generator.u_gen_l2, 'В')}</div>
                                    </div>
                                    <div class="text-center">
                                        <div class="text-xs text-gray-400">L3</div>
                                        <div class="font-mono text-sm">${formatValue(s.generator.u_gen_l3, 'В')}</div>
                                    </div>
                                </div>
                            </div>
                        </div>

                        <div class="bg-white rounded-lg p-3 shadow-sm border border-gray-100">
                            <h3 class="text-sm font-semibold text-diesel-blue mb-3 flex items-center gap-1">
                                <span class="text-lg">📈</span> Токи нагрузки
                            </h3>
                            <div class="grid grid-cols-3 gap-2">
                                <div class="bg-gray-50 p-2 rounded text-center">
                                    <div class="text-xs text-gray-500">L1</div>
                                    <div class="font-mono font-medium">${formatValue(s.current.i_l1, 'А')}</div>
                                </div>
                                <div class="bg-gray-50 p-2 rounded text-center">
                                    <div class="text-xs text-gray-500">L2</div>
                                    <div class="font-mono font-medium">${formatValue(s.current.i_l2, 'А')}</div>
                                </div>
                                <div class="bg-gray-50 p-2 rounded text-center">
                                    <div class="text-xs text-gray-500">L3</div>
                                    <div class="font-mono font-medium">${formatValue(s.current.i_l3, 'А')}</div>
                                </div>
                            </div>
                        </div>

                        <div class="bg-white rounded-lg p-3 shadow-sm border border-gray-100">
                            <h3 class="text-sm font-semibold text-diesel-blue mb-3 flex items-center gap-1">
                                <span class="text-lg">🌐</span> Сеть
                            </h3>
                            <div class="space-y-2">
                                <div class="flex justify-between items-center py-1 border-b border-gray-50">
                                    <span class="text-xs text-gray-500">Частота</span>
                                    <span class="font-mono font-medium">${formatValue(s.mains.f_mains, 'Гц')}</span>
                                </div>
                                <div class="grid grid-cols-3 gap-1 mt-2">
                                    <div class="text-center">
                                        <div class="text-xs text-gray-400">L1</div>
                                        <div class="font-mono text-sm">${formatValue(s.mains.u_mains_l1, 'В')}</div>
                                    </div>
                                    <div class="text-center">
                                        <div class="text-xs text-gray-400">L2</div>
                                        <div class="font-mono text-sm">${formatValue(s.mains.u_mains_l2, 'В')}</div>
                                    </div>
                                    <div class="text-center">
                                        <div class="text-xs text-gray-400">L3</div>
                                        <div class="font-mono text-sm">${formatValue(s.mains.u_mains_l3, 'В')}</div>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>

                ${bitsHtml}
            </div>
        `;
    }

    // ========== ГРАФИКИ ==========

    async loadParameterChart() {
        try {
            let controllerId = this.currentControllerId;
            let url;
            if (controllerId && controllerId !== '') {
                url = `/api/parameters/history?controller_id=${controllerId}&hours=${this.chartHours}`;
            } else {
                url = `/api/parameters/history?hours=${this.chartHours}`;
            }
            console.log('Загрузка графика:', url);

            const response = await fetch(url);
            if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);
            const data = await response.json();

            const ctx = document.getElementById('parameterChart')?.getContext('2d');
            if (!ctx) return;

            if (this.chart) {
                this.chart.destroy();
            }

            await this.updateChartHeader(controllerId, this.chartHours);

            const datasets = [];
            let allTimestamps = new Set();
            const selectedParam = document.getElementById('parameter-select')?.value || 'temp';
            const paramLabel = this.getParameterLabel(selectedParam);

            if (controllerId && controllerId !== '') {
                const paramKey = selectedParam;
                const paramData = data[paramKey] || [];

                if (paramData.length === 0) {
                    this.showNoDataChart(ctx, `Нет данных по параметру ${paramLabel}`);
                    return;
                }

                paramData.forEach(d => allTimestamps.add(d.ts));

                datasets.push({
                    label: paramLabel,
                    data: paramData.map(d => ({ x: d.ts, y: d.value })),
                    borderColor: this.colors[0],
                    backgroundColor: this.colors[0] + '20',
                    borderWidth: 2,
                    pointRadius: 3,
                    pointHoverRadius: 6,
                    pointBackgroundColor: this.colors[0],
                    pointBorderColor: '#002f87',
                    pointBorderWidth: 1.5,
                    tension: 0.3,
                    fill: false
                });
            } else {
                let colorIndex = 0;
                let hasData = false;
                const paramPrefix = selectedParam + '_ctrl_';

                for (const [key, value] of Object.entries(data)) {
                    if (!key.startsWith(paramPrefix) && key !== selectedParam) {
                        continue;
                    }

                    const paramData = value.data || [];
                    if (paramData.length === 0) continue;

                    hasData = true;
                    paramData.forEach(d => allTimestamps.add(d.ts));

                    const controllerName = value.name || `Установка ${value.controller_id}`;

                    datasets.push({
                        label: controllerName,
                        data: paramData.map(d => ({ x: d.ts, y: d.value })),
                        borderColor: this.colors[colorIndex % this.colors.length],
                        backgroundColor: this.colors[colorIndex % this.colors.length] + '20',
                        borderWidth: 2,
                        pointRadius: 2,
                        pointHoverRadius: 5,
                        pointBackgroundColor: this.colors[colorIndex % this.colors.length],
                        pointBorderColor: '#002f87',
                        pointBorderWidth: 1,
                        tension: 0.3,
                        fill: false
                    });
                    colorIndex++;
                }

                if (!hasData) {
                    this.showNoDataChart(ctx, `Нет данных по параметру ${paramLabel} ни для одной установки`);
                    return;
                }
            }

            if (datasets.length === 0) {
                this.showNoDataChart(ctx, 'Нет данных для отображения');
                return;
            }

            const sortedTimestamps = Array.from(allTimestamps).sort((a, b) => a - b);
            const labels = sortedTimestamps.map(ts => this.formatXAxisLabel(ts, this.chartHours));

            datasets.forEach(dataset => {
                const dataMap = new Map(dataset.data.map(d => [d.x, d.y]));
                dataset.data = sortedTimestamps.map(ts => dataMap.get(ts) || null);
            });

            this.chart = new Chart(ctx, {
                type: 'line',
                data: {
                    labels: labels,
                    datasets: datasets
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    interaction: {
                        mode: 'index',
                        intersect: false,
                    },
                    plugins: {
                        legend: {
                            display: true,
                            position: 'top',
                            labels: { font: { size: 10 } }
                        },
                        tooltip: {
                            backgroundColor: '#1f2937',
                            titleColor: '#ffffff',
                            bodyColor: '#e5e7eb',
                            borderColor: '#002f87',
                            borderWidth: 1,
                            callbacks: {
                                title: (tooltipItems) => {
                                    const index = tooltipItems[0].dataIndex;
                                    const ts = sortedTimestamps[index];
                                    if (ts) {
                                        return `📅 ${this.formatDateTimeForTooltip(ts, this.chartHours)}`;
                                    }
                                    return tooltipItems[0].label;
                                }
                            }
                        },
                        zoom: {
                            zoom: {
                                wheel: {
                                    enabled: true,
                                    speed: 0.05
                                },
                                pinch: {
                                    enabled: true
                                },
                                mode: 'x',
                                drag: false
                            },
                            pan: {
                                enabled: true,
                                mode: 'x',
                                speed: 5
                            },
                            limits: {
                                x: { min: 'original', max: 'original' }
                            }
                        }
                    },
                    scales: {
                        y: {
                            grid: { color: '#e5e7eb' },
                            ticks: { color: '#6b7280', font: { size: 10 } },
                            title: { display: true, text: this.getParameterUnit(selectedParam), font: { size: 9 } }
                        },
                        x: {
                            grid: { display: false },
                            ticks: {
                                color: '#6b7280',
                                font: { size: 9 },
                                maxRotation: 45,
                                minRotation: 45,
                                autoSkip: true,
                                maxTicksLimit: 12
                            }
                        }
                    }
                }
            });

        } catch (error) {
            console.error('Ошибка загрузки графика:', error);
            const ctx = document.getElementById('parameterChart')?.getContext('2d');
            if (ctx) {
                this.showNoDataChart(ctx, 'Ошибка загрузки данных');
            }
        }
    }

    async updateChartHeader(controllerId, hours) {
        const headerContainer = document.getElementById('chart-header');
        if (!headerContainer) return;

        let displayName = '';
        if (controllerId) {
            displayName = await this.getControllerDisplayName(controllerId);
        } else {
            displayName = 'Все установки';
        }

        let periodText = '';
        if (hours < 24 || hours > 24) {
            periodText = `Последние ${hours} часов`;
        } else if (hours === 24) {
            periodText = `Последние ${hours} часа`;
        } else {
            periodText = `Последние ${hours / 24} дней`;
        }

        headerContainer.innerHTML = `
            <div class="flex justify-between items-center flex-wrap gap-2">
                <div>
                    <span class="font-semibold text-diesel-blue">📈 Параметры</span>
                    <span class="text-gray-500 text-sm ml-1">${this.escapeHtml(displayName)}</span>
                </div>
                <div class="text-xs text-gray-400">📅 ${periodText}</div>
            </div>
        `;
    }

    getParameterLabel(param) {
        const labels = {
            'temp': 'Температура ОЖ',
            'oil_pressure': 'Давление масла',
            'fuel': 'Уровень топлива',
            'battery': 'Напряжение АКБ',
            'frequency': 'Частота сети',
            'RPM': 'Обороты'
        };
        return labels[param] || param;
    }

    getParameterUnit(param) {
        const units = {
            'temp': '°C',
            'oil_pressure': 'Бар',
            'fuel': '%',
            'battery': 'В',
            'frequency': 'Гц',
            'RPM': 'об/мин'
        };
        return units[param] || '';
    }

    showNoDataChart(ctx, message = 'Нет данных за период') {
        if (this.chart) {
            this.chart.destroy();
        }
        this.chart = new Chart(ctx, {
            type: 'line',
            data: { labels: ['Нет данных'], datasets: [{ label: message, data: [null], borderColor: '#9ca3af', borderDash: [5, 5] }] },
            options: { responsive: true, plugins: { legend: { display: true } } }
        });
    }

    // ========== ЭКСПОРТ ==========

    async exportChartAsImage() {
        let controllerId = document.getElementById('export_controller_id')?.value;

        const selectedParams = Array.from(document.querySelectorAll('input[name="export_param"]:checked'))
            .map(cb => cb.value);

        if (selectedParams.length === 0) {
            this.showToast('Выберите хотя бы один параметр', 'error');
            return;
        }

        const hours = parseInt(document.getElementById('export_hours')?.value || 24);
        const mskNow = this.getMskTime();
        const date = mskNow.toLocaleDateString('ru-RU').replace(/\./g, '-');

        this.showToast(`Загрузка ${selectedParams.length} графиков...`, 'info');

        try {
            let allData = {};

            if (controllerId) {
                const displayName = await this.getControllerDisplayName(controllerId);
                for (const param of selectedParams) {
                    const response = await fetch(`/api/parameters/history?controller_id=${controllerId}&hours=${hours}`);
                    if (!response.ok) throw new Error(`HTTP ${response.status}`);
                    const data = await response.json();
                    allData[param] = {
                        name: displayName,
                        data: data[param] || []
                    };
                }
            } else {
                const controllersResponse = await fetch('/api/controllers');
                const controllers = await controllersResponse.json();

                for (const param of selectedParams) {
                    allData[param] = {
                        name: 'Все установки',
                        datasets: []
                    };
                }

                for (const ctrl of controllers) {
                    const displayName = await this.getControllerDisplayName(ctrl.id);
                    for (const param of selectedParams) {
                        const response = await fetch(`/api/parameters/history?controller_id=${ctrl.id}&hours=${hours}`);
                        if (!response.ok) throw new Error(`HTTP ${response.status}`);
                        const data = await response.json();
                        const paramData = data[param] || [];

                        if (paramData.length > 0) {
                            allData[param].datasets.push({
                                name: displayName,
                                data: paramData
                            });
                        }
                    }
                }
            }

            const canvasWidth = 1600;
            const graphHeight = 350;
            const margin = { top: 100, right: 100, bottom: 100, left: 100 };
            const graphWidth = canvasWidth - margin.left - margin.right;

            const canvasHeight = margin.top + margin.bottom + (graphHeight * selectedParams.length) + (70 * (selectedParams.length - 1));
            const canvas = document.createElement('canvas');
            canvas.width = canvasWidth;
            canvas.height = canvasHeight;
            const ctx = canvas.getContext('2d');

            ctx.fillStyle = '#FFFFFF';
            ctx.fillRect(0, 0, canvasWidth, canvasHeight);

            ctx.fillStyle = '#002f87';
            ctx.font = 'bold 28px "Inter", sans-serif';
            ctx.textAlign = 'center';

            let title = controllerId ? `Графики параметров` : `Графики параметров - Все установки`;
            ctx.fillText(title, canvasWidth / 2, 45);

            ctx.fillStyle = '#6b7280';
            ctx.font = '14px "Inter", sans-serif';
            ctx.fillText(`Дата формирования: ${mskNow.toLocaleDateString('ru-RU')} ${mskNow.toLocaleTimeString('ru-RU')} МСК`, canvasWidth / 2, 80);

            ctx.fillStyle = '#9ca3af';
            ctx.font = '12px "Inter", sans-serif';
            let periodText = '';
            if (hours < 24 || hours > 24) periodText = `Последние ${hours} часов`;
            else if (hours === 24) periodText = `Последние ${hours} часа`;
            else periodText = `Последние ${hours / 24} дней`;
            ctx.fillText(`Период: ${periodText}`, canvasWidth / 2, 105);

            let currentY = margin.top;

            for (let idx = 0; idx < selectedParams.length; idx++) {
                const param = selectedParams[idx];
                const paramName = this.getParameterLabel(param);
                const paramUnit = this.getParameterUnit(param);

                if (controllerId) {
                    const paramData = allData[param]?.data || [];

                    if (paramData.length === 0) {
                        ctx.fillStyle = '#9ca3af';
                        ctx.font = '14px "Inter", sans-serif';
                        ctx.textAlign = 'center';
                        ctx.fillText(`Нет данных для ${paramName}`, canvasWidth / 2, currentY + graphHeight / 2);
                        currentY += graphHeight + 40;
                        continue;
                    }

                    this.drawSingleGraph(ctx, paramData, paramName, paramUnit, margin, graphWidth, graphHeight, currentY, allData[param]?.name || '');
                    currentY += graphHeight + 70;

                } else {
                    const datasets = allData[param]?.datasets || [];

                    if (datasets.length === 0) {
                        ctx.fillStyle = '#9ca3af';
                        ctx.font = '14px "Inter", sans-serif';
                        ctx.textAlign = 'center';
                        ctx.fillText(`Нет данных для ${paramName} ни у одной установки`, canvasWidth / 2, currentY + graphHeight / 2);
                        currentY += graphHeight + 40;
                        continue;
                    }

                    this.drawMultiGraph(ctx, datasets, paramName, paramUnit, margin, graphWidth, graphHeight, currentY);
                    currentY += graphHeight + 70;
                }
            }

            const image = canvas.toDataURL('image/png');
            const link = document.createElement('a');
            const fileName = controllerId ?
                `graphs_${allData[selectedParams[0]]?.name?.replace(/[^a-zа-яё0-9]/gi, '_')}_${date}.png` :
                `graphs_all_units_${date}.png`;
            link.download = fileName;
            link.href = image;
            link.click();

            this.showToast(`✅ Сохранено ${selectedParams.length} графиков в PNG`, 'success');

        } catch (error) {
            console.error('Ошибка создания графиков:', error);
            this.showToast('Ошибка при создании графиков', 'error');
        }
    }

    drawSingleGraph(ctx, paramData, paramName, paramUnit, margin, graphWidth, graphHeight, currentY, controllerName) {
        let cleanParamName = paramName;
        if (cleanParamName.includes(`(${paramUnit})`)) {
            cleanParamName = cleanParamName.replace(` (${paramUnit})`, '');
        }

        ctx.fillStyle = '#374151';
        ctx.font = 'bold 16px "Inter", sans-serif';
        ctx.textAlign = 'left';
        ctx.fillText(`${cleanParamName} (${paramUnit}) - ${controllerName}`, margin.left, currentY - 15);

        if (!paramData || paramData.length === 0) {
            ctx.fillStyle = '#9ca3af';
            ctx.font = '14px "Inter", sans-serif';
            ctx.textAlign = 'center';
            ctx.fillText('Нет данных для отображения', margin.left + graphWidth / 2, currentY + graphHeight / 2);
            return;
        }

        const values = paramData.map(d => d.value).filter(v => v !== null && v !== undefined);
        if (values.length === 0) {
            ctx.fillStyle = '#9ca3af';
            ctx.font = '14px "Inter", sans-serif';
            ctx.textAlign = 'center';
            ctx.fillText('Нет корректных данных', margin.left + graphWidth / 2, currentY + graphHeight / 2);
            return;
        }

        const minVal = Math.min(...values);
        const maxVal = Math.max(...values);
        const range = maxVal - minVal;
        const yMin = (range === 0) ? minVal - 1 : minVal - range * 0.1;
        const yMax = (range === 0) ? maxVal + 1 : maxVal + range * 0.1;

        ctx.save();
        ctx.strokeStyle = '#e5e7eb';
        ctx.lineWidth = 0.5;
        ctx.fillStyle = '#9ca3af';
        ctx.font = '10px monospace';
        ctx.textAlign = 'right';

        for (let i = 0; i <= 5; i++) {
            const y = currentY + graphHeight - (i * graphHeight / 5);
            ctx.beginPath();
            ctx.moveTo(margin.left, y);
            ctx.lineTo(margin.left + graphWidth, y);
            ctx.stroke();
            const yValue = yMin + (i * (yMax - yMin) / 5);
            ctx.fillText(yValue.toFixed(1), margin.left - 8, y + 3);
        }

        const step = graphWidth / (paramData.length - 1);

        ctx.beginPath();
        ctx.strokeStyle = '#002f87';
        ctx.lineWidth = 2.5;

        let hasValidPoint = false;
        for (let i = 0; i < paramData.length; i++) {
            const value = paramData[i].value;
            if (value === null || value === undefined) continue;

            const x = margin.left + i * step;
            const y = currentY + graphHeight - ((value - yMin) / (yMax - yMin)) * graphHeight;

            if (!hasValidPoint) {
                ctx.moveTo(x, y);
                hasValidPoint = true;
            } else {
                ctx.lineTo(x, y);
            }
        }
        ctx.stroke();

        ctx.fillStyle = '#002f87';
        const stepPoints = Math.max(1, Math.floor(paramData.length / 25));
        for (let i = 0; i < paramData.length; i += stepPoints) {
            const value = paramData[i].value;
            if (value === null || value === undefined) continue;

            const x = margin.left + i * step;
            const y = currentY + graphHeight - ((value - yMin) / (yMax - yMin)) * graphHeight;
            ctx.beginPath();
            ctx.arc(x, y, 4, 0, 2 * Math.PI);
            ctx.fill();
            ctx.fillStyle = '#002f87';
            ctx.beginPath();
            ctx.arc(x, y, 1.5, 0, 2 * Math.PI);
            ctx.fill();
        }

        ctx.fillStyle = '#6b7280';
        ctx.font = '9px monospace';
        ctx.textAlign = 'center';
        const stepLabels = Math.max(1, Math.floor(paramData.length / 10));
        for (let i = 0; i < paramData.length; i += stepLabels) {
            const x = margin.left + i * step;
            let label = '';
            if (paramData[i].ts) {
                const date = new Date(paramData[i].ts * 1000);
                label = `${date.getHours().toString().padStart(2, '0')}:${date.getMinutes().toString().padStart(2, '0')}`;
            } else if (paramData[i].timestamp) {
                label = paramData[i].timestamp;
                if (label && label.length > 8) {
                    label = label.substring(0, 5);
                }
            }
            ctx.fillText(label, x, currentY + graphHeight + 18);
        }

        ctx.restore();
    }

    drawMultiGraph(ctx, datasets, paramName, paramUnit, margin, graphWidth, graphHeight, currentY) {
        const colors = ['#002f87', '#ec6d22', '#10b981', '#8b5cf6', '#ef4444', '#06b6d4', '#f59e0b', '#ec4899'];

        let cleanParamName = paramName;
        if (cleanParamName.includes(`(${paramUnit})`)) {
            cleanParamName = cleanParamName.replace(` (${paramUnit})`, '');
        }

        ctx.fillStyle = '#374151';
        ctx.font = 'bold 16px "Inter", sans-serif';
        ctx.textAlign = 'left';
        ctx.fillText(`${cleanParamName} (${paramUnit}) - Сравнение установок`, margin.left, currentY - 25);

        let allValues = [];
        let allTimestamps = new Set();
        datasets.forEach(ds => {
            ds.data.forEach(d => {
                if (d.value !== null && d.value !== undefined) {
                    allValues.push(d.value);
                    if (d.ts) allTimestamps.add(d.ts);
                }
            });
        });

        if (allValues.length === 0) {
            ctx.fillStyle = '#9ca3af';
            ctx.font = '14px "Inter", sans-serif';
            ctx.textAlign = 'center';
            ctx.fillText('Нет данных для отображения', margin.left + graphWidth / 2, currentY + graphHeight / 2);
            return;
        }

        const minVal = Math.min(...allValues);
        const maxVal = Math.max(...allValues);
        const range = maxVal - minVal;
        const yMin = (range === 0) ? minVal - 1 : minVal - range * 0.1;
        const yMax = (range === 0) ? maxVal + 1 : maxVal + range * 0.1;

        ctx.save();
        ctx.strokeStyle = '#e5e7eb';
        ctx.lineWidth = 0.5;
        ctx.fillStyle = '#9ca3af';
        ctx.font = '10px monospace';
        ctx.textAlign = 'right';

        for (let i = 0; i <= 5; i++) {
            const y = currentY + graphHeight - (i * graphHeight / 5);
            ctx.beginPath();
            ctx.moveTo(margin.left, y);
            ctx.lineTo(margin.left + graphWidth, y);
            ctx.stroke();
            const yValue = yMin + (i * (yMax - yMin) / 5);
            ctx.fillText(yValue.toFixed(1), margin.left - 8, y + 3);
        }

        const sortedTimestamps = Array.from(allTimestamps).sort((a, b) => a - b);
        if (sortedTimestamps.length < 2) {
            ctx.fillStyle = '#9ca3af';
            ctx.font = '14px "Inter", sans-serif';
            ctx.textAlign = 'center';
            ctx.fillText('Недостаточно точек для построения графика', margin.left + graphWidth / 2, currentY + graphHeight / 2);
            ctx.restore();
            return;
        }

        const step = graphWidth / (sortedTimestamps.length - 1);

        datasets.forEach((ds, idx) => {
            const dataMap = new Map(ds.data.map(d => [d.ts, d.value]));

            ctx.beginPath();
            ctx.strokeStyle = colors[idx % colors.length];
            ctx.lineWidth = 2;

            let firstPoint = true;
            for (let i = 0; i < sortedTimestamps.length; i++) {
                const ts = sortedTimestamps[i];
                const value = dataMap.get(ts);
                if (value === undefined || value === null) continue;

                const x = margin.left + i * step;
                const y = currentY + graphHeight - ((value - yMin) / (yMax - yMin)) * graphHeight;

                if (firstPoint) {
                    ctx.moveTo(x, y);
                    firstPoint = false;
                } else {
                    ctx.lineTo(x, y);
                }
            }
            ctx.stroke();
        });

        ctx.font = '11px "Inter", sans-serif';
        const legendStartY = currentY - 5;
        const legendLineHeight = 18;

        const itemsPerColumn = Math.ceil(datasets.length / 2);
        const legendColumnWidth = graphWidth / 2 - 20;

        datasets.forEach((ds, idx) => {
            let column = 0;
            let row = idx;
            if (idx >= itemsPerColumn) {
                column = 1;
                row = idx - itemsPerColumn;
            }

            const legendX = margin.left + 10 + (column * legendColumnWidth);
            const legendY = legendStartY + (row * legendLineHeight);

            if (legendY < currentY + graphHeight - 10) {
                ctx.fillStyle = colors[idx % colors.length];
                ctx.fillRect(legendX, legendY - 10, 12, 12);

                ctx.fillStyle = '#374151';
                ctx.textAlign = 'left';
                let shortName = ds.name;
                if (shortName.length > 28) shortName = shortName.substring(0, 25) + '...';
                ctx.fillText(shortName, legendX + 18, legendY);
            }
        });

        ctx.fillStyle = '#6b7280';
        ctx.font = '9px monospace';
        ctx.textAlign = 'center';
        const stepLabels = Math.max(1, Math.floor(sortedTimestamps.length / 10));
        for (let i = 0; i < sortedTimestamps.length; i += stepLabels) {
            const x = margin.left + i * step;
            const date = new Date(sortedTimestamps[i] * 1000);
            const label = `${date.getHours().toString().padStart(2, '0')}:${date.getMinutes().toString().padStart(2, '0')}`;
            ctx.fillText(label, x, currentY + graphHeight + 18);
        }

        ctx.restore();
    }

    async exportChartAsExcel() {
        const controllerId = document.getElementById('export_controller_id')?.value;
        const hours = document.getElementById('export_hours')?.value || 24;
        const selectedParams = Array.from(document.querySelectorAll('input[name="export_param"]:checked'))
            .map(cb => cb.value);

        if (selectedParams.length === 0) {
            this.showToast('Выберите хотя бы один параметр', 'error');
            return;
        }

        let url = `/api/export/charts?hours=${hours}&format=xlsx`;
        if (controllerId) {
            url += `&controller_id=${controllerId}`;
        }
        url += `&params=${selectedParams.join(',')}`;

        this.showToast(`Экспорт данных (${selectedParams.length} параметров) в Excel...`, 'info');

        const link = document.createElement('a');
        link.href = url;
        link.style.display = 'none';
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);

        setTimeout(() => {
            this.showToast(`Экспорт ${selectedParams.length} параметров в Excel завершен`, 'success');
        }, 1000);
    }

    showExportModal(type) {
        this.exportType = type;
        const modal = document.getElementById('exportModal');
        const modalTitle = document.getElementById('modalTitle');
        const modalContent = document.getElementById('modalContent');

        if (!modal || !modalContent) return;

        let title = '';
        let content = '';

        if (type === 'events') {
            title = '📋 Экспорт журнала событий';
            content = this.getEventsExportForm();
        } else if (type === 'charts') {
            title = '📈 Экспорт графиков';
            content = this.getChartsExportForm();
        } else if (type === 'report') {
            title = '📊 Экспорт полного отчета';
            content = this.getReportExportForm();
        }

        modalTitle.textContent = title;
        modalContent.innerHTML = content;
        modal.classList.add('active');

        const confirmBtn = document.getElementById('exportConfirmBtn');
        if (confirmBtn) {
            confirmBtn.onclick = () => this.performExport();
        }

        if (type === 'charts') {
            setTimeout(() => {
                const selectAllBtn = document.getElementById('select-all-params');
                const deselectAllBtn = document.getElementById('deselect-all-params');

                if (selectAllBtn) {
                    selectAllBtn.onclick = () => {
                        document.querySelectorAll('input[name="export_param"]').forEach(cb => cb.checked = true);
                    };
                }
                if (deselectAllBtn) {
                    deselectAllBtn.onclick = () => {
                        document.querySelectorAll('input[name="export_param"]').forEach(cb => cb.checked = false);
                    };
                }
            }, 50);
        }
    }

    getEventsExportForm() {
        let controllerOptions = '<option value="">Все установки</option>';
        this.controllers.forEach(ctrl => {
            const selected = (this.currentControllerId == ctrl.id) ? 'selected' : '';
            const displayName = this.namesCache[ctrl.id] || ctrl.name || 'ДГУ';
            controllerOptions += `<option value="${ctrl.id}" ${selected}>${this.escapeHtml(displayName)}</option>`;
        });

        const maxDate = this.getMaxDate();
        const defaultStartDate = this.getDateDaysAgo(7);
        const defaultEndDate = this.currentDate;

        return `
            <div class="export-option">
                <label>📌 Установка</label>
                <select id="export_controller_id">
                    ${controllerOptions}
                </select>
            </div>
            <div class="export-option">
                <label>📅 Дата начала</label>
                <input type="date" id="export_start_date" value="${defaultStartDate}" max="${maxDate}">
            </div>
            <div class="export-option">
                <label>📅 Дата окончания</label>
                <input type="date" id="export_end_date" value="${defaultEndDate}" max="${maxDate}">
            </div>
            <div class="export-option">
                <label>📄 Формат файла</label>
                <div class="radio-group">
                    <label><input type="radio" name="export_format" value="csv" checked> CSV</label>
                    <label><input type="radio" name="export_format" value="xlsx"> Excel (XLSX)</label>
                </div>
            </div>
        `;
    }

    getChartsExportForm() {
        let controllerOptions = '<option value="">🌍 Все установки</option>';
        this.controllers.forEach(ctrl => {
            const selected = (this.currentControllerId == ctrl.id) ? 'selected' : '';
            const displayName = this.namesCache[ctrl.id] || ctrl.name || 'ДГУ';
            controllerOptions += `<option value="${ctrl.id}" ${selected}>${this.escapeHtml(displayName)}</option>`;
        });

        const parameters = [
            { id: 'temp', name: '🌡️ Температура ОЖ', checked: true },
            { id: 'oil_pressure', name: '⚙️ Давление масла', checked: true },
            { id: 'fuel', name: '⛽ Уровень топлива', checked: true },
            { id: 'battery', name: '🔋 Напряжение АКБ', checked: true },
            { id: 'frequency', name: '📡 Частота сети', checked: true },
            { id: 'RPM', name: '🔄 Обороты', checked: true }
        ];

        let paramsHtml = '<div class="flex justify-between items-center mb-2">';
        paramsHtml += '<span class="text-xs text-gray-500">Выберите параметры:</span>';
        paramsHtml += '<div class="flex gap-2">';
        paramsHtml += '<button type="button" id="select-all-params" class="text-xs text-diesel-blue hover:underline">Выбрать все</button>';
        paramsHtml += '<button type="button" id="deselect-all-params" class="text-xs text-gray-500 hover:underline">Снять все</button>';
        paramsHtml += '</div></div>';
        paramsHtml += '<div class="grid grid-cols-2 gap-2 mb-3" id="params-container">';
        parameters.forEach(param => {
            paramsHtml += `
                <label class="flex items-center gap-2 text-xs">
                    <input type="checkbox" name="export_param" value="${param.id}" ${param.checked ? 'checked' : ''}>
                    ${param.name}
                </label>
            `;
        });
        paramsHtml += '</div>';

        return `
            <div class="export-option">
                <label>📌 Установка</label>
                <select id="export_controller_id" required>
                    ${controllerOptions}
                </select>
            </div>
            <div class="export-option">
                <label>⏱️ Период</label>
                <select id="export_hours">
                    <option value="6">Последние 6 часов</option>
                    <option value="12">Последние 12 часов</option>
                    <option value="24" selected>Последние 24 часа</option>
                    <option value="48">Последние 48 часов</option>
                    <option value="72">Последние 3 дня</option>
                    <option value="168">Последние 7 дней</option>
                </select>
            </div>
            <div class="export-option">
                ${paramsHtml}
            </div>
            <div class="export-option">
                <label>📄 Тип экспорта</label>
                <div class="radio-group">
                    <label><input type="radio" name="export_chart_type" value="image" checked> 📸 Сохранить как картинку (PNG)</label>
                </div>
                <div class="radio-group mt-2">
                    <label><input type="radio" name="export_chart_type" value="data"> 📊 Данные в Excel</label>
                </div>
            </div>
            <div class="text-xs text-blue-600 mt-2">💡 При выборе "Все установки" будут показаны графики для всех установок</div>
        `;
    }

    getReportExportForm() {
        let controllerOptions = '<option value="">Выберите установку</option>';
        this.controllers.forEach(ctrl => {
            const selected = (this.currentControllerId == ctrl.id) ? 'selected' : '';
            const displayName = this.namesCache[ctrl.id] || ctrl.name || 'ДГУ';
            controllerOptions += `<option value="${ctrl.id}" ${selected}>${this.escapeHtml(displayName)}</option>`;
        });

        const maxDate = this.getMaxDate();
        const defaultStartDate = this.getDateDaysAgo(7);
        const defaultEndDate = this.currentDate;

        return `
            <div class="export-option">
                <label>📌 Установка *</label>
                <select id="export_controller_id" required>
                    ${controllerOptions}
                </select>
            </div>
            <div class="export-option">
                <label>📅 Дата начала</label>
                <input type="date" id="export_start_date" value="${defaultStartDate}" max="${maxDate}">
            </div>
            <div class="export-option">
                <label>📅 Дата окончания</label>
                <input type="date" id="export_end_date" value="${defaultEndDate}" max="${maxDate}">
            </div>
            <div class="text-xs text-blue-600 mt-2">💡 Полный отчет включает: журнал событий, данные графиков, текущий статус и статистику</div>
        `;
    }

    getDateDaysAgo(days) {
        const mskNow = this.getMskTime();
        const date = new Date(mskNow);
        date.setDate(date.getDate() - days);
        return date.toISOString().split('T')[0];
    }

    async downloadFile(url, fileName, successMessage, errorMessage) {
        try {
            const response = await fetch(url);
            if (!response.ok) throw new Error(`HTTP ${response.status}`);

            const blob = await response.blob();
            const link = document.createElement('a');
            link.href = URL.createObjectURL(blob);
            link.download = fileName;
            link.click();
            URL.revokeObjectURL(link.href);

            this.showToast(successMessage, 'success');
        } catch (error) {
            console.error('Ошибка:', error);
            this.showToast(errorMessage, 'error');
        }
    }

    async performExport() {
        if (this.exportType === 'events') {
            const controllerId = document.getElementById('export_controller_id')?.value;
            const format = document.querySelector('input[name="export_format"]:checked')?.value || 'csv';
            const startDate = document.getElementById('export_start_date')?.value;
            const endDate = document.getElementById('export_end_date')?.value;

            // Валидация дат
            const maxDate = this.getMaxDate();
            if (startDate && startDate > maxDate) {
                this.showToast('Дата начала не может быть в будущем', 'error');
                return;
            }
            if (endDate && endDate > maxDate) {
                this.showToast('Дата окончания не может быть в будущем', 'error');
                return;
            }
            if (startDate && endDate && startDate > endDate) {
                this.showToast('Дата начала не может быть позже даты окончания', 'error');
                return;
            }

            let url = `/api/export/events?format=${format}`;
            if (controllerId) url += `&controller_id=${controllerId}`;
            if (startDate) url += `&start_date=${startDate}`;
            if (endDate) url += `&end_date=${endDate}`;

            this.showToast('Начинаю экспорт событий...', 'info');

            await this.downloadFile(
                url,
                `events_${new Date().toISOString().slice(0, 19)}.${format}`,
                'Экспорт событий успешно завершен',
                'Ошибка при экспорте событий'
            );

        } else if (this.exportType === 'charts') {
            const exportType = document.querySelector('input[name="export_chart_type"]:checked')?.value;
            if (exportType === 'image') {
                await this.exportChartAsImage();
            } else {
                await this.exportChartAsExcel();
            }

        } else if (this.exportType === 'report') {
            const controllerId = document.getElementById('export_controller_id')?.value;
            if (!controllerId) {
                this.showToast('Пожалуйста, выберите установку', 'error');
                return;
            }
            const startDate = document.getElementById('export_start_date')?.value;
            const endDate = document.getElementById('export_end_date')?.value;

            // Валидация дат
            const maxDate = this.getMaxDate();
            if (startDate && startDate > maxDate) {
                this.showToast('Дата начала не может быть в будущем', 'error');
                return;
            }
            if (endDate && endDate > maxDate) {
                this.showToast('Дата окончания не может быть в будущем', 'error');
                return;
            }
            if (startDate && endDate && startDate > endDate) {
                this.showToast('Дата начала не может быть позже даты окончания', 'error');
                return;
            }

            let url = `/api/export/complete-report/${controllerId}`;
            const params = new URLSearchParams();
            if (startDate) params.append('start_date', startDate);
            if (endDate) params.append('end_date', endDate);
            if (params.toString()) url += `?${params.toString()}`;

            const displayName = await this.getControllerDisplayName(controllerId);

            this.showToast('Начинаю формирование полного отчета...', 'info');

            await this.downloadFile(
                url,
                `report_${displayName.replace(/[^a-zа-яё0-9]/gi, '_')}_${new Date().toISOString().slice(0, 19)}.xlsx`,
                'Полный отчет успешно сформирован',
                'Ошибка при формировании отчета'
            );
        }

        closeExportModal();
    }

    showToast(message, type = 'info') {
        let toastContainer = document.getElementById('toast-container');
        if (!toastContainer) {
            toastContainer = document.createElement('div');
            toastContainer.id = 'toast-container';
            toastContainer.style.cssText = `
                position: fixed;
                bottom: 1rem;
                right: 1rem;
                z-index: 50;
                display: flex;
                flex-direction: column;
                gap: 0.5rem;
            `;
            document.body.appendChild(toastContainer);
        }

        const toast = document.createElement('div');
        toast.className = `px-4 py-2 rounded-lg shadow-lg text-white text-sm ${
            type === 'error' ? 'bg-red-600' : 'bg-green-600'
        }`;
        toast.style.opacity = '0';
        toast.style.transform = 'translateX(100%)';
        toast.style.transition = 'all 0.3s ease';
        toast.textContent = message;

        toastContainer.appendChild(toast);

        setTimeout(() => {
            toast.style.opacity = '1';
            toast.style.transform = 'translateX(0)';
        }, 10);

        setTimeout(() => {
            toast.style.opacity = '0';
            toast.style.transform = 'translateX(100%)';
            setTimeout(() => {
                if (toast.parentNode) {
                    toast.parentNode.removeChild(toast);
                }
                if (toastContainer.children.length === 0) {
                    toastContainer.remove();
                }
            }, 300);
        }, 3000);
    }

    setupEventListeners() {
        const filterSelect = document.getElementById('controller-filter');
        if (filterSelect) {
            filterSelect.addEventListener('change', (e) => {
                this.currentControllerId = e.target.value;
                console.log('📌 Смена установки на:', this.currentControllerId || 'Все установки');
                this.loadNotifications();
                this.loadParameterChart();
                this.loadCurrentStatus();
                this.loadCurrentAlerts();
                this.adjustNotificationsHeight();
            });
        }

        const dateFilter = document.getElementById('date-filter');
        if (dateFilter) {
            dateFilter.value = this.currentDate;
            dateFilter.max = this.getMaxDate();
            dateFilter.onchange = (e) => {
                this.currentDate = e.target.value;
                this.loadNotifications();
            };
        }

        const chartPeriodSelect = document.getElementById('chart-period-select');
        if (chartPeriodSelect) {
            chartPeriodSelect.addEventListener('change', (e) => {
                this.chartHours = parseInt(e.target.value);
                this.loadParameterChart();
            });
        }

        const paramSelect = document.getElementById('parameter-select');
        if (paramSelect) {
            paramSelect.addEventListener('change', () => this.loadParameterChart());
        }

        const exportEventsBtn = document.getElementById('export-events-btn');
        if (exportEventsBtn) exportEventsBtn.onclick = () => this.showExportModal('events');

        const exportChartsBtn = document.getElementById('export-charts-btn');
        if (exportChartsBtn) exportChartsBtn.onclick = () => this.showExportModal('charts');

        const exportReportBtn = document.getElementById('export-full-report-btn');
        if (exportReportBtn) exportReportBtn.onclick = () => this.showExportModal('report');

        const todayBtn = document.getElementById('today-btn');
        if (todayBtn) {
            todayBtn.onclick = () => {
                this.currentDate = this.getTodayDate();
                const df = document.getElementById('date-filter');
                if (df) df.value = this.currentDate;
                this.loadNotifications();
            };
        }

        const tabEvents = document.getElementById('tab-events');
        const tabStatus = document.getElementById('tab-status');
        const eventsContainer = document.getElementById('events-container');
        const statusContainer = document.getElementById('status-container');
        const eventsFilter = document.getElementById('events-filter');

        if (tabEvents && tabStatus) {
            tabEvents.onclick = () => {
                tabEvents.className = 'flex-1 px-5 py-3 text-sm font-medium transition-colors border-b-2 border-diesel-blue text-diesel-blue';
                tabStatus.className = 'flex-1 px-5 py-3 text-sm font-medium transition-colors border-b-2 border-transparent text-gray-500';
                eventsContainer.classList.remove('hidden');
                statusContainer.classList.add('hidden');
                if (eventsFilter) eventsFilter.classList.remove('hidden');
                this.currentTab = 'events';
                this.adjustNotificationsHeight();
            };

            tabStatus.onclick = () => {
                tabStatus.className = 'flex-1 px-5 py-3 text-sm font-medium transition-colors border-b-2 border-diesel-blue text-diesel-blue';
                tabEvents.className = 'flex-1 px-5 py-3 text-sm font-medium transition-colors border-b-2 border-transparent text-gray-500';
                eventsContainer.classList.add('hidden');
                statusContainer.classList.remove('hidden');
                if (eventsFilter) eventsFilter.classList.add('hidden');
                this.currentTab = 'status';
                this.loadCurrentStatus();
            };
        }

        const exportActBtn = document.getElementById('export-act');
        if (exportActBtn) exportActBtn.onclick = () => this.generateServiceAct();

        const sendBtn = document.getElementById('send-message');
        const chatInput = document.getElementById('chat-input');
        if (sendBtn && chatInput) {
            sendBtn.onclick = () => this.sendMessage();
            chatInput.onkeypress = (e) => { if (e.key === 'Enter') this.sendMessage(); };
        }

        const orderBtn = document.getElementById('order-button');
        if (orderBtn) orderBtn.onclick = () => window.open('https://www.comd.ru', '_blank');
    }



async sendMessage() {
        const input = document.getElementById('chat-input');
        const question = input.value.trim();

        if (!question) return;

        // Отображаем вопрос пользователя
        this.addChatMessage(question, 'user');
        input.value = '';

        // Показываем индикатор загрузки
        this.showTypingIndicator();

        // Получаем текущие данные установки
        const monitoringData = await this.getLLMMonitoringData();

        try {
            const response = await fetch('/api/llm/ask', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    question: question,
                    monitoring_data: monitoringData
                })
            });

            const result = await response.json();

            // Убираем индикатор загрузки
            this.hideTypingIndicator();

            if (result.success) {
                this.addChatMessage(result.answer, 'assistant');

                if (result.sources && result.sources.length > 0) {
                    this.showSources(result.sources);
                }
            } else {
                this.addChatMessage(result.answer, 'assistant');
            }
        } catch (error) {
            this.hideTypingIndicator();
            this.addChatMessage('❌ Ошибка подключения к AI помощнику. Проверьте, что сервер запущен.', 'assistant');
            console.error('LLM error:', error);
        }
    }

    async getLLMMonitoringData() {
        let controllerId = this.currentControllerId;

        if (!controllerId && this.controllers.length > 0) {
            controllerId = this.controllers[0].id;
        }

        if (!controllerId) return {};

        try {
            const response = await fetch(`/api/status/${controllerId}`);
            const data = await response.json();

            if (data.status_data) {
                const s = data.status_data;
                return {
                    unit_name: s.unit_name,
                    rpm: s.engine?.rpm !== '—' ? parseFloat(s.engine.rpm) : null,
                    temp: s.sensors?.temp !== '—' ? parseFloat(s.sensors.temp) : null,
                    oil: s.sensors?.oil !== '—' ? parseFloat(s.sensors.oil) : null,
                    battery: s.sensors?.battery !== '—' ? parseFloat(s.sensors.battery) : null,
                    fuel: s.sensors?.fuel !== '—' ? parseFloat(s.sensors.fuel) : null,
                    mode: s.general?.mains_state || 'НОРМАЛЬНЫЙ'
                };
            }
            return {};
        } catch (error) {
            console.error('Ошибка получения данных для LLM:', error);
            return {};
        }
    }

    addChatMessage(text, role) {
        const container = document.getElementById('chat-messages');
        if (!container) return;

        const messageDiv = document.createElement('div');
        messageDiv.className = `chat-message p-2 rounded-lg text-xs mb-1 ${
            role === 'user'
                ? 'bg-diesel-blue text-white ml-4'
                : 'bg-gray-100 text-gray-700 mr-4'
        }`;
        // Добавляем перенос длинных строк
        messageDiv.innerHTML = this.formatLLMMessage(this.wrapText(text, 60));
        container.appendChild(messageDiv);
        container.scrollTop = container.scrollHeight;
    }

    wrapText(text, maxLength = 60) {
        if (!text) return '';

        const lines = [];
        let currentLine = '';
        const words = text.split(' ');

        for (const word of words) {
            if (word.length > maxLength) {
                if (currentLine.trim()) {
                    lines.push(currentLine.trim());
                    currentLine = '';
                }
                for (let i = 0; i < word.length; i += maxLength) {
                    lines.push(word.slice(i, i + maxLength));
                }
            } else if ((currentLine + ' ' + word).length > maxLength) {
                if (currentLine.trim()) {
                    lines.push(currentLine.trim());
                }
                currentLine = word;
            } else {
                currentLine += (currentLine ? ' ' : '') + word;
            }
        }

        if (currentLine.trim()) {
            lines.push(currentLine.trim());
        }

        return lines.join('<br>');
    }

    // Получение истории диалога
    getConversationHistory() {
        const container = document.getElementById('chat-messages');
        if (!container) return [];

        const messages = [];
        const messageDivs = container.querySelectorAll('.chat-message');

        messageDivs.forEach(div => {
            const isUser = div.classList.contains('bg-diesel-blue');
            const text = div.innerText || div.textContent;

            if (text.includes('Агент думает') || text.includes('готов отвечать')) {
                return;
            }

            messages.push({
                role: isUser ? 'user' : 'assistant',
                text: text
            });
        });

        return messages;
    }

    // Генерация акта
    async generateServiceAct(type = 'service') {
        try {
            const displayName = await this.getControllerDisplayName(this.currentControllerId);

            const monitoringData = await this.getLLMMonitoringData();
            const conversationHistory = this.getConversationHistory();

            let endpoint = '/api/generate-report';
            let fileName = '';

            if (type === 'service') {
                fileName = `Акт_обслуживания_${displayName.replace(/[^a-zа-яё0-9]/gi, '_')}.docx`;
            } else if (type === 'claim') {
                fileName = `Акт_Заявка_${displayName.replace(/[^a-zа-яё0-9]/gi, '_')}.docx`;
            } else {
                fileName = `Заказ_наряд_${displayName.replace(/[^a-zа-яё0-9]/gi, '_')}.docx`;
            }

            const response = await fetch(endpoint, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    report_type: type,
                    monitoring_data: monitoringData,
                    conversation_history: conversationHistory,
                    controller_id: this.currentControllerId
                })
            });

            const blob = await response.blob();
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = fileName;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(url);

            this.showToast(`✅ ${fileName} сформирован`, 'success');

        } catch (error) {
            console.error('Ошибка:', error);
            this.showToast('❌ Ошибка при формировании акта', 'error');
        }
    }

    // Инициализация выпадающего меню
    initDropdowns() {
        const exportBtn = document.getElementById('export-act-btn');
        const exportMenu = document.getElementById('export-act-menu');

        if (exportBtn && exportMenu) {
            exportBtn.onclick = (e) => {
                e.stopPropagation();
                exportMenu.classList.toggle('hidden');
            };

            document.addEventListener('click', () => {
                exportMenu.classList.add('hidden');
            });
        }

        const serviceBtn = document.getElementById('export-act-service');
        const claimBtn = document.getElementById('export-act-claim');
        const workBtn = document.getElementById('export-work-order');

        if (serviceBtn) serviceBtn.onclick = () => this.generateServiceAct('service');
        if (claimBtn) claimBtn.onclick = () => this.generateServiceAct('claim');
        if (workBtn) workBtn.onclick = () => this.generateServiceAct('work_order');
    }

    formatLLMMessage(text) {
        let html = this.escapeHtml(text);
        // Жирный текст
        html = html.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
        // Курсив
        html = html.replace(/\*(.*?)\*/g, '<em>$1</em>');
        // Списки
        html = html.replace(/^• /gm, '<br>• ');
        html = html.replace(/^- /gm, '<br>- ');
        // Нумерация
        html = html.replace(/(\d+\.) /g, '<br>$1 ');
        // Переносы строк
        html = html.replace(/\n/g, '<br>');
        return html;
    }

    showSources(sources) {
        const container = document.getElementById('chat-messages');
        if (!container) return;

        const sourcesDiv = document.createElement('div');
        sourcesDiv.className = 'p-2 rounded-lg text-xs bg-blue-50 text-blue-600 mr-4 mb-1 mt-0.5';
        sourcesDiv.innerHTML = '📚 <strong>Источники:</strong> ' + sources.map(s => s.source).join(', ');

        container.appendChild(sourcesDiv);
        container.scrollTop = container.scrollHeight;
    }

    showTypingIndicator() {
        const container = document.getElementById('chat-messages');
        if (!container) return;

        const typingDiv = document.createElement('div');
        typingDiv.id = 'typing-indicator';
        typingDiv.className = 'p-2 rounded-lg text-xs bg-gray-100 text-gray-500 mr-4 mb-1';
        typingDiv.innerHTML = '🤔 Агент думает <span class="typing-dots">...</span>';
        container.appendChild(typingDiv);
        container.scrollTop = container.scrollHeight;
    }

    hideTypingIndicator() {
        const typingDiv = document.getElementById('typing-indicator');
        if (typingDiv) typingDiv.remove();
    }

    async initLLM() {
        console.log('🔄 Проверка LLM агента...');
        try {
            const response = await fetch('/api/llm/status');
            const status = await response.json();

            if (status.status === 'ready') {
                console.log('✅ LLM агент готов');
                this.addChatMessage('✅ AI помощник готов отвечать на вопросы по документации', 'assistant');
            } else {
                console.warn('⚠️ LLM агент не готов');
                this.addChatMessage('⚠️ AI помощник временно недоступен. Проверьте настройки.', 'assistant');
            }
        } catch (error) {
            console.error('❌ Ошибка подключения к LLM агенту:', error);
            this.addChatMessage('❌ AI помощник недоступен. Убедитесь, что сервер запущен.', 'assistant');
        }
    }

    updateClock() {
        const mskNow = this.getMskTime();
        const timeString = mskNow.toLocaleTimeString('ru-RU');
        const timeSmall = document.getElementById('current-time-small');
        if (timeSmall) timeSmall.textContent = timeString;
    }

    getMaxDate() {
        return this.getTodayDate();
    }

    escapeHtml(str) {
        if (!str) return '';
        return str.replace(/[&<>]/g, function(m) {
            if (m === '&') return '&amp;';
            if (m === '<') return '&lt;';
            if (m === '>') return '&gt;';
            return m;
        });
    }
}

// Запуск
document.addEventListener('DOMContentLoaded', () => {
    console.log('📄 DOM загружен');
    window.app = new DieselMonitoring();
});