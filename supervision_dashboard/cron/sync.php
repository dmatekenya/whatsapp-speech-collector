<?php
declare(strict_types=1);
// Hostinger cron PHP CLI only. Refuse execution from HTTP.
if(PHP_SAPI!=='cli'){http_response_code(404);exit;}
require dirname(__DIR__).'/app/bootstrap.php';
require dirname(__DIR__).'/app/integration.php';
try{$r=sync_production();echo 'OK '.gmdate('c').' stored='.$r['stored'].' imported='.$r['rows'].PHP_EOL;}
catch(Throwable $e){sync_failed('Scheduled HTTPS production sync failed');error_log('SpeechQuality cron sync: '.$e->getMessage());fwrite(STDERR,'FAIL: authorized production sync unavailable'.PHP_EOL);exit(1);}
