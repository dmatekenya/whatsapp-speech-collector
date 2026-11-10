<?php
declare(strict_types=1);
if(PHP_SAPI!=='cli'){http_response_code(404);exit;}
require __DIR__.'/../app/bootstrap.php';
require __DIR__.'/../app/integration.php';
require __DIR__.'/../app/reviews.php';
$c=cfg();
echo 'Metadata API URL: '.(!empty($c['api_url'])?'configured':'missing').PHP_EOL;
echo 'QC token: '.(!empty($c['review_api_token'])&&strlen((string)$c['review_api_token'])>=48?'configured':'missing').PHP_EOL;
try {
 $status=official_write_status();
 echo 'DigitalOcean QC write-back: '.strtoupper($status).PHP_EOL;
 if($status==='ready')echo "QC endpoint is connected. Refresh the online voice page to see Approve and Reject.\n";
 else echo "No official recording update can be sent until the DigitalOcean endpoint reports READY.\n";
 exit($status==='ready'?0:2);
}catch(Throwable $e){fwrite(STDERR,"QC connectivity check failed without changing records. Inspect server error logs.\n");exit(2);}
