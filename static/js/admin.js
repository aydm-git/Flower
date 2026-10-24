(function () {
  'use strict';

  // Подтверждение берётся из data-confirm как текст: названия товаров с кавычками
  // больше не ломают JavaScript и не могут выполнить чужой код.
  document.querySelectorAll('form[data-confirm]').forEach(function (form) {
    form.addEventListener('submit', function (event) {
      if (!window.confirm(form.dataset.confirm)) event.preventDefault();
    });
  });

  document.querySelectorAll('select[data-autosubmit]').forEach(function (select) {
    select.addEventListener('change', function () {
      if (select.form) select.form.submit();
    });
  });
})();
