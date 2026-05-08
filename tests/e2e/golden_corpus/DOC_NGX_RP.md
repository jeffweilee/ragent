# Nginx Reverse Proxy

An Nginx reverse proxy sits in front of one or more upstream application
servers, accepting client requests on the public side and forwarding
them to the appropriate backend. This pattern centralizes TLS
termination, request routing, caching, and rate limiting.

## Configuring a Reverse Proxy

A minimal reverse proxy block:

```nginx
upstream api_backend {
    least_conn;
    server 10.0.0.11:8080 max_fails=3 fail_timeout=10s;
    server 10.0.0.12:8080 max_fails=3 fail_timeout=10s;
}

server {
    listen 443 ssl http2;
    server_name api.example.com;

    ssl_certificate /etc/ssl/api.crt;
    ssl_certificate_key /etc/ssl/api.key;

    location / {
        proxy_pass http://api_backend;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 60s;
    }
}
```

## Best Practices

- Set `proxy_set_header Host $host` so virtual-host routing on the
  backend keeps working.
- Forward client IP and original scheme via `X-Real-IP` /
  `X-Forwarded-*` so backends can log accurately.
- Tune `proxy_buffering` for streaming endpoints — disable for SSE.
- Terminate TLS at the proxy and use HTTP to the backend on a private
  network.

## Troubleshooting

- `502 Bad Gateway` typically means the upstream is unreachable or
  returned an empty response. Check `error_log`.
- `504 Gateway Timeout` means `proxy_read_timeout` was exceeded; raise
  it for slow endpoints or fix the backend.
- `nginx -t` validates configuration before reload; never reload
  without it.
