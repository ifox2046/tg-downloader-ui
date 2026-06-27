'use strict';
'require view';

return view.extend({
	render: function() {
		var url = 'http://' + window.location.hostname + ':9910/';

		window.setTimeout(function() {
			window.location.href = url;
		}, 50);

		return E('div', { 'class': 'cbi-section' }, [
			E('h2', {}, 'Telegram Downloads'),
			E('p', {}, 'Opening Telegram Download Manager...'),
			E('p', {}, [
				E('a', {
					'class': 'btn cbi-button cbi-button-apply',
					'href': url
				}, 'Open manager')
			])
		]);
	}
});
