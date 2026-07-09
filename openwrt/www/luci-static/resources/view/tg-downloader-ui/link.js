'use strict';
'require view';
'require rpc';
'require ui';

var SERVICE_NAME = 'tg-downloader-ui';
var isReadonlyView = !L.hasViewPermission();

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

	load: function() {
		return this.callRcList();
	},

	handleServiceAction: function(action) {
		return this.callRcInit(SERVICE_NAME, action).then(function(ret) {
			if (ret)
				throw new Error(_('Command failed'));

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
		var service = rcList[SERVICE_NAME] || {};
		var running = service.running === true;
		var enabled = service.enabled === true;

		return E('div', { 'class': 'cbi-section' }, [
			E('h2', {}, 'Telegram Downloads'),
			E('p', {}, running
				? _('Service is running.')
				: _('Service is stopped.')),
			E('p', {}, _('Boot startup: %s').format(enabled ? _('enabled') : _('disabled'))),
			E('div', { 'class': 'cbi-page-actions' }, [
				E('a', {
					'class': 'btn cbi-button cbi-button-apply',
					'href': url
				}, _('Open manager')),
				E('button', {
					'class': 'btn cbi-button cbi-button-action',
					'click': ui.createHandlerFn(this, 'handleServiceAction', 'start'),
					'disabled': isReadonlyView || running
				}, _('Start')),
				E('button', {
					'class': 'btn cbi-button cbi-button-action',
					'click': ui.createHandlerFn(this, 'handleServiceAction', 'restart'),
					'disabled': isReadonlyView
				}, _('Restart')),
				E('button', {
					'class': 'btn cbi-button cbi-button-negative',
					'click': ui.createHandlerFn(this, 'handleServiceAction', 'stop'),
					'disabled': isReadonlyView || !running
				}, _('Stop'))
			])
		]);
	},

	handleSaveApply: null,
	handleSave: null,
	handleReset: null
});
