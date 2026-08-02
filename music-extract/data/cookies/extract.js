(async () => {
    if (!window.cookieStore) {
      console.error("CookieStore API no soportada en este navegador.");
      return;
    }
    
    const cookies = await cookieStore.getAll();
    let netscapeFormat = "# Netscape HTTP Cookie File\n# http://curl.haxx.se/rfc/cookie_spec.html\n# This is a generated file!  Do not edit.\n\n";
    
    cookies.forEach(c => {
      const domain = c.domain.startsWith('.') ? c.domain : '.' + c.domain;
      const includeSubdomains = "TRUE";
      const path = c.path || "/";
      const secure = c.secure ? "TRUE" : "FALSE";
      const expires = c.expires ? Math.floor(c.expires / 1000) : 0;
      netscapeFormat += `${domain}\t${includeSubdomains}\t${path}\t${secure}\t${expires}\t${c.name}\t${c.value}\n`;
    });
  
    // Crear y disparar la descarga automática del archivo
    const blob = new Blob([netscapeFormat], { type: 'text/plain' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'cookies.txt';
    a.click();
    console.log("Archivo cookies.txt generado y descargado.");
  })();