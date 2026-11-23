# Comunicaciones Wascar — Web RGB funcional (solo pruebas)

Se conserva el diseño RGB y la fecha 30 de diciembre de 2026. El formulario envía solicitudes a Flask y PostgreSQL. Cada solicitud recibe dos números **ficticios** entre 00001 y 10000; no hay integración con boletas físicas, ni cobros reales, ni emisión de participaciones válidas.

## Publicación en Railway
1. Crea un repositorio **privado** de GitHub y sube los archivos de esta carpeta a la raíz.
2. En Railway crea un proyecto independiente, añade un servicio PostgreSQL y conecta el repositorio GitHub.
3. Configura `DATABASE_URL` referenciando la variable de conexión del servicio PostgreSQL; `ADMIN_USER` y `ADMIN_PASSWORD` con credenciales únicas y robustas. No las escribas en el repositorio.
4. Railway instalará `requirements.txt` y usará el inicio de `railway.json`. En Settings > Networking genera dominio HTTPS.
5. Abre `/` para la web RGB, `/admin` para panel de pruebas y `/health` para estado.

## Límites y seguridad
- Solo utilizar **datos ficticios**; la demo guarda formularios en PostgreSQL y no cuenta con política de privacidad ni autorización para recolectar información real.
- HTTP Basic es únicamente para pruebas; antes de uso real: autenticación con sesiones y 2FA, CSRF, rate limiting, auditoría, cifrado de respaldos, retención de datos, validación de identidad y revisión de seguridad.
- Números ficticios no se sincronizan con los boletos impresos; no usar para vender.
- El estado aprobado no confirma dinero ni habilita participación real.
- Antes de operar, verificar autorizaciones de rifas y disponibilidad de pasarela de pagos legalmente apta.
