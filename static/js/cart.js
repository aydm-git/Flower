(function () {
  'use strict';

  const STORAGE_KEY = 'flowerCart';
  const MAX_QTY = 99;

  const summary = document.getElementById('cart-summary');
  const cartList = document.getElementById('cart-list');
  const cartTotal = document.getElementById('cart-total');
  const flowerInput = document.getElementById('flower-input');
  const cartField = document.getElementById('cart-field');
  const orderForm = document.getElementById('order-form');
  const submitBtn = document.getElementById('submit-btn');
  const clearBtn = document.getElementById('clear-cart-btn');
  const toastContainer = document.getElementById('toast-container');

  const catalog = {};
  document.querySelectorAll('.add-to-cart').forEach((btn) => {
    catalog[btn.dataset.id] = {
      id: btn.dataset.id,
      name: btn.dataset.name,
      price: parseInt(btn.dataset.price, 10),
      img: btn.dataset.img,
      stock: parseInt(btn.dataset.stock, 10) || 0,
    };
  });

  let memoryFallback = [];

  function readStored() {
    let raw = memoryFallback;
    try {
      const parsed = JSON.parse(localStorage.getItem(STORAGE_KEY));
      if (Array.isArray(parsed)) raw = parsed;
    } catch (e) {
    }

    const items = [];
    raw.forEach((item) => {
      const qty = parseInt(item && item.quantity, 10);
      if (!item || item.id === undefined || !(qty > 0)) return;
      items.push({ id: String(item.id), quantity: Math.min(qty, MAX_QTY) });
    });
    return items;
  }

  function writeStored(items) {
    memoryFallback = items;
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(items));
    } catch (e) {
    }
  }

  function loadCart() {
    const cart = [];
    readStored().forEach((item) => {
      const product = catalog[item.id];
      if (!product) return;
      const limit = Math.min(MAX_QTY, product.stock);
      if (limit < 1) return;
      cart.push({ ...product, quantity: Math.min(item.quantity, limit) });
    });
    return cart;
  }

  function saveCart(cart) {
    writeStored(cart.map((i) => ({ id: i.id, quantity: i.quantity })));
  }

  function showToast(message) {
    if (!toastContainer) return;
    const toast = document.createElement('div');
    toast.className = 'toast';

    const icon = document.createElement('span');
    icon.textContent = '🌸';
    const text = document.createElement('span');
    text.textContent = message;

    toast.append(icon, text);
    toastContainer.appendChild(toast);
    setTimeout(() => toast.remove(), 3000);
  }

  function addToCart(id) {
    const product = catalog[id];
    if (!product) return;

    const limit = Math.min(MAX_QTY, product.stock);
    const items = readStored();
    const existing = items.find((i) => i.id === id);
    if (existing) {
      if (existing.quantity >= limit) {
        showToast(`«${product.name}»: больше нет в наличии`);
        return;
      }
      existing.quantity += 1;
    } else {
      items.push({ id, quantity: 1 });
    }
    writeStored(items);
    showToast(`«${product.name}» добавлен в корзину!`);
    renderCart();
  }

  function changeQuantity(id, delta) {
    let cart = loadCart();
    const item = cart.find((i) => i.id === id);
    if (!item) return;

    item.quantity = Math.min(item.quantity + delta, Math.min(MAX_QTY, item.stock));
    if (item.quantity <= 0) {
      cart = cart.filter((i) => i.id !== id);
    }
    saveCart(cart);
    renderCart();
  }

  function clearCart() {
    writeStored([]);
    try {
      localStorage.removeItem(STORAGE_KEY);
    } catch (e) {
    }
    renderCart();
  }

  function formatMoney(n) {
    return n.toLocaleString('ru-RU');
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function qtyButton(label, id, delta) {
    const b = el('button', '', label);
    b.type = 'button';
    b.dataset.action = 'qty';
    b.dataset.id = id;
    b.dataset.delta = String(delta);
    b.setAttribute('aria-label', delta > 0 ? 'Добавить ещё один' : 'Убрать один');
    return b;
  }

  function renderCart() {
    if (!summary || !cartList) return;

    const cart = loadCart();
    saveCart(cart);

    if (cartField) {
      // Цена отправляется, чтобы сервер отклонил заказ, если она изменилась
      // после загрузки страницы и покупатель не видел новую сумму.
      cartField.value = JSON.stringify(
        cart.map((i) => ({ id: i.id, quantity: i.quantity, price: i.price }))
      );
    }

    if (cart.length === 0) {
      summary.classList.add('cart-summary-hidden');
      if (flowerInput) flowerInput.value = '';
      return;
    }

    summary.classList.remove('cart-summary-hidden');
    cartList.textContent = '';

    let total = 0;
    const names = [];

    cart.forEach((item) => {
      const lineSum = item.price * item.quantity;
      total += lineSum;
      names.push(`${item.name} (${item.quantity} шт)`);

      const row = el('div', 'cart-item');

      const img = document.createElement('img');
      img.src = item.img;
      img.alt = item.name;

      const info = el('div', 'cart-item-info');
      info.append(el('h4', '', item.name), el('div', 'price', `${formatMoney(item.price)} ₽`));

      const controls = el('div', 'cart-item-controls');
      controls.append(
        qtyButton('−', item.id, -1),
        el('span', '', String(item.quantity)),
        qtyButton('+', item.id, 1)
      );

      const sum = el('div', 'cart-item-sum', `${formatMoney(lineSum)} ₽`);

      row.append(img, info, controls, sum);
      cartList.appendChild(row);
    });

    if (cartTotal) cartTotal.textContent = formatMoney(total);
    if (flowerInput) flowerInput.value = names.join(', ');
  }

  document.querySelectorAll('.add-to-cart').forEach((btn) => {
    btn.addEventListener('click', () => addToCart(btn.dataset.id));
  });

  if (cartList) {
    cartList.addEventListener('click', (e) => {
      const btn = e.target.closest('button[data-action="qty"]');
      if (!btn) return;
      changeQuantity(btn.dataset.id, parseInt(btn.dataset.delta, 10));
    });
  }

  if (clearBtn) clearBtn.addEventListener('click', clearCart);

  if (orderForm) {
    orderForm.addEventListener('submit', (e) => {
      const cart = loadCart();
      if (cart.length === 0) {
        e.preventDefault();
        showToast('Корзина пуста — выберите букет в каталоге');
        const catalogSection = document.getElementById('catalog');
        if (catalogSection) catalogSection.scrollIntoView({ behavior: 'smooth' });
        return;
      }
      renderCart();
      if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.textContent = 'Отправляем…';
      }
    });
  }

  if (document.querySelector('[data-clear-cart]')) {
    writeStored([]);
    try {
      localStorage.removeItem(STORAGE_KEY);
    } catch (e) {
    }
  }

  window.addEventListener('pageshow', () => {
    if (submitBtn) {
      submitBtn.disabled = false;
      submitBtn.textContent = 'Отправить заявку';
    }
    renderCart();
  });

  renderCart();
})();
