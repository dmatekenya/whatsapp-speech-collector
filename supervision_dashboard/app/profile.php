<?php
declare(strict_types=1);
/** Private profile images. JPEG response is authenticated; files remain beneath protected private/. */
function profile_photo_dir(): string { return dirname(__DIR__).'/private/profile_images'; }
function profile_photo_file(int $id): string { return profile_photo_dir().'/avatar_'.max(1,$id).'.jpg'; }
function profile_photo_available(array $user): bool { return is_file(profile_photo_file((int)$user['id'])); }
function avatar_markup(array $user,string $size='small'): string {
    $id=(int)$user['id'];
    $name=(string)$user['display_name'];
    $initial=function_exists('mb_substr')?mb_substr($name,0,1):substr($name,0,1);
    if(profile_photo_available($user)) {
        $url=linkto('avatar',['uid'=>$id,'v'=>(int)filemtime(profile_photo_file($id))]);
        return '<span class="user-avatar '.h($size).' has-photo"><img src="'.h($url).'" alt="'.h($name).' profile photo" loading="lazy"></span>';
    }
    return '<span class="user-avatar '.h($size).'">'.h(strtoupper($initial)).'</span>';
}
function profile_photo_serve(array $viewer,int $id): never {
    if($id!==(int)$viewer['id']) { http_response_code(403);exit('Forbidden'); }
    $path=profile_photo_file($id);
    if(!is_file($path)) { http_response_code(404);exit('Image not found'); }
    header('Content-Type: image/jpeg');
    header('Cache-Control: private, max-age=300');
    header('X-Content-Type-Options: nosniff');
    header('Content-Length: '.(string)filesize($path));
    readfile($path);exit;
}
function profile_photo_upload(array $user): void {
    $uploaded=$_FILES['profile_photo']??null;
    if(!is_array($uploaded) || ($uploaded['error']??UPLOAD_ERR_NO_FILE)!==UPLOAD_ERR_OK) {
        throw new RuntimeException('Choose an image under 2 MB and try again.');
    }
    if(!is_uploaded_file((string)$uploaded['tmp_name']) || (int)$uploaded['size']>2097152 || (int)$uploaded['size']<100) {
        throw new RuntimeException('The selected image must be smaller than 2 MB.');
    }
    $info=@getimagesize((string)$uploaded['tmp_name']);
    if(!$info || ($info[0]??0)<120 || ($info[1]??0)<120 || ($info[0]*$info[1])>20000000) {
        throw new RuntimeException('Choose a valid image at least 120 pixels wide and tall.');
    }
    if(!extension_loaded('gd')) throw new RuntimeException('The hosting PHP GD image extension must be enabled.');
    $type=(int)($info[2]??0);
    if(!in_array($type,[IMAGETYPE_JPEG,IMAGETYPE_PNG,IMAGETYPE_WEBP],true)) throw new RuntimeException('Only JPG, PNG and WebP photos are accepted.');
    $source=match($type){
        IMAGETYPE_JPEG=>@imagecreatefromjpeg($uploaded['tmp_name']),
        IMAGETYPE_PNG=>@imagecreatefrompng($uploaded['tmp_name']),
        IMAGETYPE_WEBP=>function_exists('imagecreatefromwebp')?@imagecreatefromwebp($uploaded['tmp_name']):false,
        default=>false,
    };
    if(!$source) throw new RuntimeException('The image could not be opened. Try a different JPG or PNG photo.');
    $width=imagesx($source);$height=imagesy($source);
    $targetRatio=350/450;
    $cropWidth=$width;$cropHeight=$height;$startX=0;$startY=0;
    if(($width/$height)>$targetRatio){$cropWidth=(int)round($height*$targetRatio);$startX=(int)floor(($width-$cropWidth)/2);}
    else{$cropHeight=(int)round($width/$targetRatio);$startY=(int)floor(($height-$cropHeight)/2);}
    $dest=imagecreatetruecolor(350,450);
    imagefill($dest,0,0,imagecolorallocate($dest,255,255,255));
    if(!imagecopyresampled($dest,$source,0,0,$startX,$startY,350,450,$cropWidth,$cropHeight)){imagedestroy($source);imagedestroy($dest);throw new RuntimeException('Image processing failed.');}
    imagedestroy($source);
    $dir=profile_photo_dir();
    if(!is_dir($dir) && !@mkdir($dir,0700,true)) {imagedestroy($dest); throw new RuntimeException('Photo storage is not writable on this hosting account.');}
    @chmod($dir,0700);
    $temp=tempnam($dir,'avatar_tmp_');
    if(!$temp) {imagedestroy($dest);throw new RuntimeException('Photo storage is not writable.');}
    try {
        if(!imagejpeg($dest,$temp,88)) throw new RuntimeException('Could not save the processed photo.');
        @chmod($temp,0600);
        if(!@rename($temp,profile_photo_file((int)$user['id']))) throw new RuntimeException('Could not update the profile photo.');
        @chmod(profile_photo_file((int)$user['id']),0600);
    } finally { imagedestroy($dest); if(is_file($temp)) @unlink($temp); }
}
