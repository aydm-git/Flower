document.addEventListener('DOMContentLoaded', () => {
    renderCart();
});

function showToast(message) {
    const container = document.getElementById('toast-container');
    const toast = document.createElement('div');
    toast.className = 'toast';
    toast.innerHTML = `<span>🌸</span> ${message}`;
    container.appendChild(toast);

    setTimeout(() => {
        toast.remove();
    }, 3000);
}

document.querySelectorAll('.add-to-cart').forEach(button => {
    button.addEventListener('click', () => {
        const id = button.dataset.id;
        const name = button.dataset.name;
        const price = parseInt(button.dataset.price);
        const img = button.dataset.img;

        let cart = JSON.parse(localStorage.getItem('flowerCart')) || [];
        
        const existingItem = cart.find(item => item.id === id);
        if (existingItem) {
            existingItem.quantity += 1;
        } else {
            cart.push({ id, name, price, img, quantity: 1 });
        }

        localStorage.setItem('flowerCart', JSON.stringify(cart));
        showToast(`«${name}» добавлен в корзину!`);
        renderCart();
    });
});

function changeQuantity(id, delta) {
    let cart = JSON.parse(localStorage.getItem('flowerCart')) || [];
    const item = cart.find(i => i.id === id);
    
    if (item) {
        item.quantity += delta;
        if (item.quantity <= 0) {
            cart = cart.filter(i => i.id !== id);
        }
        localStorage.setItem('flowerCart', JSON.stringify(cart));
        renderCart();
    }
}

function renderCart() {
    const cart = JSON.parse(localStorage.getItem('flowerCart')) || [];
    const cartSummary = document.getElementById('cart-summary');
    const cartList = document.getElementById('cart-list');
    const cartTotal = document.getElementById('cart-total');
    const flowerInput = document.getElementById('flower-input');

    if (cart.length === 0) {
        cartSummary.classList.add('cart-summary-hidden');
        flowerInput.value = '';
        return;
    }

    cartSummary.classList.remove('cart-summary-hidden');
    cartList.innerHTML = '';
    
    let total = 0;
    let flowerString = '';

    cart.forEach(item => {
        total += item.price * item.quantity;
        flowerString += `${item.name} (${item.quantity} шт), `;
        
        const div = document.createElement('div');
        div.className = 'cart-item';
        div.innerHTML = `
            <img src="${item.img}" alt="${item.name}">
            <div class="cart-item-info">
                <h4>${item.name}</h4>
                <div class="price">${item.price} ₽</div>
            </div>
            <div class="cart-item-controls">
                <button onclick="changeQuantity('${item.id}', -1)">-</button>
                <span>${item.quantity}</span>
                <button onclick="changeQuantity('${item.id}', 1)">+</button>
            </div>
            <div style="font-weight: bold; width: 80px; text-align: right;">
                ${item.price * item.quantity} ₽
            </div>
        `;
        cartList.appendChild(div);
    });

    cartTotal.textContent = total;
    flowerString = flowerString.slice(0, -2);
    flowerInput.value = flowerString;
}

function clearCart() {
    localStorage.removeItem('flowerCart');
    renderCart();
}