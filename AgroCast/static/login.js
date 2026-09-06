const form = document.getElementById('login-form');
const status = document.getElementById('login-status');
const button = document.getElementById('login-submit');
const password = document.getElementById('password');

form.addEventListener('submit', async event => {
    event.preventDefault();
    if (location.protocol !== 'https:') {
        status.textContent = 'Откройте сервис по HTTPS. Пароль не был отправлен.';
        return;
    }
    button.disabled = true;
    status.textContent = 'Проверяем учётную запись…';
    try {
        const response = await fetch('/api/auth/login', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            credentials: 'same-origin',
            signal: AbortSignal.timeout(15000),
            body: JSON.stringify({username: document.getElementById('username').value.trim(), password: password.value})
        });
        password.value = '';
        if (response.ok) {
            location.replace('/');
            return;
        }
        const messages = {
            401: 'Не удалось войти. Проверьте имя и пароль или обратитесь к администратору.',
            403: 'Адрес страницы не соответствует настройке HTTPS-сервиса.',
            422: 'Проверьте формат имени пользователя и пароля.',
            429: 'Слишком много попыток. Подождите минуту перед повторным входом.',
            503: 'Вход временно недоступен. Администратору нужно проверить PostgreSQL и миграции.'
        };
        status.textContent = messages[response.status] || 'Войти не удалось. Повторите позже.';
    } catch {
        password.value = '';
        status.textContent = 'Сервер не ответил. Проверьте соединение и повторите вход.';
    } finally {
        button.disabled = false;
    }
});
