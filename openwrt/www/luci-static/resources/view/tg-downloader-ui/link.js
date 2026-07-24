'use strict';
'require view';
'require rpc';
'require ui';

var SERVICE_NAME = 'tg-downloader-ui';

return view.extend({
	callRcList: rpc.declare({
		object: 'rc',
		method: 'list',
		expect: { '': {} }
	}),

	callRcInit: rpc.declare({
		object: 'rc',
		method: 'init',
		params: [ 'name', 'action' ]
	}),

	// Same path as System → Startup (works with luci-mod-system-init ACLs).
	callLuciInitAction: rpc.declare({
		object: 'luci',
		method: 'setInitAction',
		params: [ 'name', 'action' ],
		expect: { result: false }
	}),

	isReadonlyView: function() {
		// Match stock startup.js: false must become null so E() omits the
		// disabled attribute. A literal false can still grey-out buttons.
		// Evaluate at call time — module-load L.hasViewPermission() races ACL hydration.
		try {
			if (typeof L === 'undefined' || typeof L.hasViewPermission !== 'function')
				return null;
			return L.hasViewPermission() ? null : true;
		}
		catch (e) {
			return null;
		}
	},

	load: function() {
		return this.callRcList().catch(function() {
			return {};
		});
	},

	handleServiceAction: function(action) {
		var self = this;

		// Prefer setInitAction first (same as System → Startup on iStoreOS).
		return this.callLuciInitAction(SERVICE_NAME, action).then(function(success) {
			if (success === true)
				return true;
			throw new Error('setInitAction declined');
		}).catch(function() {
			return self.callRcInit(SERVICE_NAME, action).then(function(ret) {
				// rc.init returns null / empty on success; only treat real errors as failure.
				if (ret === true || ret === 0 || ret === null || ret === undefined || ret === '')
					return true;
				if (typeof ret === 'object' && ret !== null && !Object.keys(ret).length)
					return true;
				throw new Error(_('Command failed'));
			});
		}).then(function() {
			ui.addNotification(null, E('p', _('Service action "%s" was sent.').format(action)), 'info');
			window.setTimeout(function() {
				window.location.reload();
			}, 1000);
		}).catch(function(e) {
			ui.addNotification(null, E('p', _('Failed to execute service action "%s": %s').format(action, e.message || e)));
		});
	},

	render: function(rcList) {
		var url = 'http://' + window.location.hostname + ':9910/';
		var service = (rcList && rcList[SERVICE_NAME]) || {};
		var running = service.running === true;
		var enabled = service.enabled === true;
		// null when writable (omit attribute); true when readonly.
		var readonly = this.isReadonlyView();

		return E('div', { 'class': 'cbi-section' }, [
			E('h2', {}, 'Telegram Downloads'),
			E('p', {}, running
				? _('Service is running.')
				: _('Service is stopped.')),
			E('p', {}, _('Boot startup: %s').format(enabled ? _('enabled') : _('disabled'))),
			E('div', { 'class': 'cbi-page-actions' }, [
				E('a', {
					'class': 'btn cbi-button cbi-button-apply',
					'href': url,
					'target': '_blank',
					'rel': 'noopener'
				}, _('Open manager')),
				E('button', {
					'class': 'btn cbi-button-action',
					'click': ui.createHandlerFn(this, 'handleServiceAction', 'start'),
					'disabled': readonly || (running ? true : null)
				}, _('Start')),
				E('button', {
					'class': 'btn cbi-button-action',
					'click': ui.createHandlerFn(this, 'handleServiceAction', 'restart'),
					'disabled': readonly
				}, _('Restart')),
				E('button', {
					'class': 'btn cbi-button-negative',
					'click': ui.createHandlerFn(this, 'handleServiceAction', 'stop'),
					'disabled': readonly || (running ? null : true)
				}, _('Stop')),
				E('button', {
					'class': 'btn cbi-button-action',
					'click': ui.createHandlerFn(this, 'handleServiceAction', enabled ? 'disable' : 'enable'),
					'disabled': readonly
				}, enabled ? _('Disable') : _('Enable'))
			])
		]);
	},

	handleSaveApply: null,
	handleSave: null,
	handleReset: null
});
