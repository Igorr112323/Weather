let csrfToken = null;

export class ApiError extends Error {
    constructor(message, status) {
        super(message);
        this.status = status;
    }
}

export async function request(path, options = {}) {
    if (!path.startsWith('/api/') || new URL(path, location.origin).origin !== location.origin) throw new Error('Only same-origin API requests are allowed');
    const method = options.method || 'GET';
    const headers = new Headers(options.headers);
    if (options.body !== undefined) headers.set('Content-Type', 'application/json');
    if (!['GET', 'HEAD'].includes(method) && csrfToken) headers.set('X-CSRF-Token', csrfToken);
    const response = await fetch(path, {
        ...options, method, headers, credentials: 'same-origin',
        signal: options.signal || AbortSignal.timeout(15000),
        body: options.body === undefined ? undefined : JSON.stringify(options.body)
    });
    if (response.status === 204) return null;
    let payload;
    try {
        payload = await response.json();
    } catch {
        throw new ApiError('Сервер вернул неподдерживаемый ответ.', response.status);
    }
    if (!response.ok) {
        if (response.status === 401) location.replace('/login');
        const message = typeof payload.error === 'string' ? payload.error : response.status === 422 ? 'Проверьте заполненные значения.' : 'Запрос не выполнен.';
        throw new ApiError(message, response.status);
    }
    return payload;
}

export async function session() {
    const response = await request('/api/auth/me');
    csrfToken = response.csrf_token;
    return response.user;
}

export async function logout() {
    await request('/api/auth/logout', {method: 'POST'});
    csrfToken = null;
    location.replace('/login');
}
