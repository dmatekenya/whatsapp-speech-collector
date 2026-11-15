<?php
// Standalone, read-only CLI smoke tests; never accessible over HTTP.
if(PHP_SAPI!=='cli') {http_response_code(404);exit;}
require __DIR__.'/bootstrap.php';
require __DIR__.'/view.php';
require __DIR__.'/pages.php';
require __DIR__.'/integration.php';
require __DIR__.'/reviews.php';
function check(bool $condition,string $message): void {if(!$condition){fwrite(STDERR,"FAIL: $message\n");exit(1);}echo "PASS: $message\n";}
check(APP_VERSION==='0.0.0.1','Version remains fixed');
check(APP_TARGET_HOURS===500,'500-hour project goal');
[$from,$to]=ml_day_bounds('2026-11-04');
check($from==='2026-11-03 22:00:00' && $to==='2026-11-04 22:00:00','Malawi UTC+02 calendar boundaries');
check(str_contains(h('<script>'),'&lt;script&gt;'),'HTML escaping');
check(str_contains(linkto('voices',['qc'=>'needs_review']),'qc=needs_review'),'QC filter navigation');
check(date_ml('2026-11-10 10:40:00')==='10 Nov 2026, 12:40','Malawi timestamp presentation');
check(file_exists(__DIR__.'/schema.php') && count(require __DIR__.'/schema.php')>=10,'Database schema included');
check(is_file(dirname(__DIR__).'/private/.htaccess') && str_contains(file_get_contents(dirname(__DIR__).'/private/.htaccess'),'Require all denied'),'Private PHP configuration folder is web-protected');
check(function_exists('production_review_gateway') && function_exists('new_operation_key'),'Production QC confirmation module included');
echo "Source CLI smoke tests complete. Live MySQL test is performed on Hostinger during installation.\n";
