async function loadPilot() {
    const status = document.getElementById('status');
    try {
        const response = await fetch('/api/capabilities', {signal: AbortSignal.timeout(10000)});
        if (!response.ok) throw new Error('Матрица возможностей недоступна');
        const capabilities = await response.json();
        document.getElementById('validation').textContent = capabilities.validation.warning;
        const body = document.getElementById('points');
        body.replaceChildren();
        for (const region of capabilities.regions) {
            for (const point of region.inspection_points) {
                const row = document.createElement('tr');
                for (const value of [point.id, point.lat.toFixed(2), point.lon.toFixed(2), 'Проверка методики']) {
                    const cell = document.createElement('td');
                    cell.textContent = value;
                    row.append(cell);
                }
                body.append(row);
            }
        }
        status.textContent = 'Политика ' + capabilities.policy_version + ' · новые прогнозы отключены · без агрорекомендаций';
    } catch {
        status.textContent = 'Не удалось загрузить матрицу возможностей. Расчёты остаются отключёнными. Обновите страницу позже.';
    }
}

loadPilot();

let csrfToken = null;

async function loadIdentity() {
    const status = document.getElementById('identity-status');
    try {
        const response = await fetch('/api/auth/me', {credentials: 'same-origin', signal: AbortSignal.timeout(10000)});
        if (response.status === 401) {
            location.replace('/login');
            return;
        }
        if (!response.ok) throw new Error('Identity unavailable');
        const data = await response.json();
        csrfToken = data.csrf_token;
        status.textContent = data.user.username + ' · роль: ' + data.user.role;
    } catch {
        status.textContent = 'Не удалось проверить учётную запись. Повторите вход позже.';
    }
}

document.getElementById('logout').addEventListener('click', async event => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
        if (!csrfToken) throw new Error('Session unavailable');
        const response = await fetch('/api/auth/logout', {
            method: 'POST', credentials: 'same-origin', headers: {'X-CSRF-Token': csrfToken},
            signal: AbortSignal.timeout(10000)
        });
        if (response.ok || response.status === 401) {
            csrfToken = null;
            location.replace('/login');
            return;
        }
        throw new Error('Logout unavailable');
    } catch {
        document.getElementById('identity-status').textContent = 'Выход не подтверждён сервером. Повторите попытку.';
    } finally {
        button.disabled = false;
    }
});

loadIdentity();
