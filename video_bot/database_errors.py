"""Turn database failures into useful messages without printing credentials."""
from urllib.parse import urlsplit


def database_error_hint(error, url=''):
    text = str(error).lower()
    state = getattr(error, 'sqlstate', None)
    if '[your-password]' in url.lower():
        return 'DATABASE_URL still contains [YOUR-PASSWORD]. Replace it with the database password, without brackets.'
    if state == '28P01' or 'password authentication failed' in text:
        return 'Database login failed. Check the database password and URL-encode special characters in it. Use the username from Supabase Connect > Session pooler.'
    if 'tenant or user not found' in text:
        return 'Supabase could not identify the project/user. Copy the complete Session pooler URI, including its postgres.PROJECT_REFERENCE username.'
    try:
        parsed = urlsplit(url)
        direct_supabase = (parsed.hostname or '').startswith('db.') and (parsed.hostname or '').endswith('.supabase.co')
    except ValueError:
        return 'DATABASE_URL is malformed. Copy the complete Session pooler URI and URL-encode special characters in the password.'
    if direct_supabase and any(word in text for word in ('network is unreachable', 'resolve', 'timeout', 'timed out', 'could not translate')):
        return 'The Supabase direct database address could not be reached. In Supabase Connect, choose Session pooler (port 5432) and replace DATABASE_URL with that complete URI.'
    if any(word in text for word in ('could not translate host', 'could not resolve', 'name or service not known', 'nodename nor servname')):
        return 'The database hostname could not be resolved. Check that the connection string is complete and copied from the correct Supabase project.'
    if state == '42501' or 'permission denied' in text:
        return 'The database user lacks permission to create or access the bot tables. Use the project database credentials from Supabase Connect.'
    if any(word in text for word in ('ssl', 'certificate')):
        return 'The secure database connection failed. Use the Supabase connection URI with sslmode=require and check the project SSL settings.'
    if any(word in text for word in ('timeout', 'timed out', 'connection refused', 'network is unreachable')):
        return 'The database is unreachable or timed out. Confirm the Supabase project is active, use Session pooler on port 5432, and check network restrictions.'
    if any(word in text for word in ('invalid', 'missing', 'uri', 'port')):
        return 'Check the complete DATABASE_URL format. Do not include surrounding quotes or brackets; URL-encode special characters in the password.'
    return 'Database connection or table initialization failed. Check the Supabase project status, Session pooler URI, credentials, and table permissions.'
