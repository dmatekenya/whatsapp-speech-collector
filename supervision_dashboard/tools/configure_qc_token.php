<?php
declare(strict_types=1);
// Use via `bash tools/configure_qc_token.sh` in Hostinger Terminal.
if(PHP_SAPI!=='cli'){http_response_code(404);exit;}
$secret=trim((string)(getenv('MASAKHANE_QC_REVIEW_TOKEN')?:''));
if(!preg_match('/^[0-9a-f]{96}$/D',$secret)){
 fwrite(STDERR,"QC token is missing/invalid. The DigitalOcean owner-approved setup creates a 96-character private hex token.\n");exit(2);
}
$root=realpath(__DIR__.'/../private');
if($root===false||!is_file($root.'/config.php')||!is_file($root.'/.htaccess')){
 fwrite(STDERR,"Existing private/config.php and private/.htaccess are required. No changes made.\n");exit(2);
}
$destination=$root.'/qc_review_token.php';
if(is_link($destination)){
 fwrite(STDERR,"Refusing symlink destination. No changes made.\n");exit(2);
}
$old=is_file($destination)?file_get_contents($destination):null;
$backup=null;
if($old!==null){$backup=$root.'/qc_token_backup_'.gmdate('Ymd_His').'.php';if(!copy($destination,$backup)){fwrite(STDERR,"Backup failed. No changes made.\n");exit(2);}chmod($backup,0600);}
$temp=tempnam($root,'.qc-review-');
if(!$temp){fwrite(STDERR,"Cannot create temporary secret file.\n");exit(2);}
$contents="<?php\ndeclare(strict_types=1);\nreturn ".var_export($secret,true).";\n";
if(file_put_contents($temp,$contents,LOCK_EX)===false||!chmod($temp,0600)||!rename($temp,$destination)){
 @unlink($temp);fwrite(STDERR,"Secret file update failed; old credential preserved if present.\n");exit(2);
}
if(!is_readable($destination)){
 fwrite(STDERR,"QC token file was created but is not readable by the terminal user. Check PHP user permissions.\n");exit(3);
}
echo "Private QC token saved without revealing it. No recording status changed.\n";
