<?php
/** Copy to private/config.php on your server; DO NOT COMMIT the real config. */
declare(strict_types=1);
return [
    'db_host' => 'localhost',
    'db_port' => 3306,
    'db_name' => 'YOUR_HOSTINGER_DATABASE',
    'db_user' => 'YOUR_DATABASE_USER',
    'db_pass' => 'SET_OUTSIDE_GIT',
    'api_url' => 'https://YOUR_AUTHORIZED_PRODUCTION_GATEWAY',
    'api_token' => 'SET_OUTSIDE_GIT',
    'api_timeout' => 25,
    'review_api_token' => '', // Owner-approved QC gateway only, disabled by default.
];
