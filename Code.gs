// ==========================================
// 1. ROUTING & OBERFLÄCHE (GUI)
// ==========================================

function doGet(e) {
  // Wenn in der URL "?page=admin" steht, lade das Admin-Dashboard
  if (e && e.parameter && e.parameter.page === 'admin') {
    return HtmlService.createHtmlOutputFromFile('Admin')
        .setTitle('Smart Parking - Projekt anlegen')
        .setXFrameOptionsMode(HtmlService.XFrameOptionsMode.ALLOWALL)
        .addMetaTag('viewport', 'width=device-width, initial-scale=1');
  } 
  
  // Standardmäßig (ohne Parameter) die Techniker-App (Uploader) laden
  return HtmlService.createHtmlOutputFromFile('Index')
      .setTitle('Smart Parking - Uploader')
      .setXFrameOptionsMode(HtmlService.XFrameOptionsMode.ALLOWALL)
      .addMetaTag('viewport', 'width=device-width, initial-scale=1');
}


// ==========================================
// 2. FUNKTIONEN FÜR DIE TECHNIKER-APP (INDEX)
// ==========================================

// Holt die Projektdaten aus dem Google Sheet (Spalte B bis I)
function getProjects() {
  try {
    var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName('Projektliste');
    var lastRow = sheet.getLastRow();
    if (lastRow < 2) return []; 
    
    // Wir lesen ab Spalte B (Spalte 2) insgesamt 8 Spalten ein (Spalten B bis I)
    var data = sheet.getRange(2, 2, lastRow - 1, 8).getValues();
    
    return data.map(function(row) {
      return {
        name: row[0] ? row[0].toString().trim() : "",             // Spalte B: Projektname
        id: row[1] ? row[1].toString().trim() : "",               // Spalte C: Ordner ID
        ukLink: row[2] ? row[2].toString().trim() : "",           // Spalte D: UK Link (PDF)
        ansprechpartner: row[3] ? row[3].toString().trim() : "",  // Spalte E: Ansprechpartner
        telefon: row[4] ? row[4].toString().trim() : "",          // Spalte F: Telefon
        email: row[5] ? row[5].toString().trim() : "",            // Spalte G: Email
        adresse: row[6] ? row[6].toString().trim() : "",          // Spalte H: Adresse
        notizen: row[7] ? row[7].toString().trim() : ""           // Spalte I: Notizen
      };
    }).filter(function(proj) {
      return proj.name !== "" && proj.id !== "";
    });
  } catch (e) {
    return [];
  }
}

// Holt alle bereits existierenden Bilder aus dem spezifischen Projektordner
function getExistingImages(folderIdFromForm) {
  try {
    var parentFolderId = '10I98qFF59FnUQjKv5l3bKxwmHVYrlpXV'; // Hauptordner für die Bilder
    var parentFolder = DriveApp.getFolderById(parentFolderId);
    
    var folderIterator = parentFolder.getFoldersByName(folderIdFromForm);
    if (!folderIterator.hasNext()) {
      return [];
    }
    
    var targetFolder = folderIterator.next();
    var files = targetFolder.getFiles();
    var imagesList = [];
    
    while (files.hasNext()) {
      var file = files.next();
      var mimeType = file.getMimeType();
      
      if (mimeType.indexOf('image/') === 0) {
        imagesList.push({
          name: file.getName(),
          url: file.getUrl(),
          id: file.getId()
        });
      }
    }
    
    imagesList.sort(function(a, b) {
      return a.name.localeCompare(b.name);
    });
    
    return imagesList;
    
  } catch (error) {
    return [];
  }
}

// Verarbeitet den Datei-Upload (Bilder der Techniker) in den Projektordner
function uploadFile(fileData, folderIdFromForm) {
  try {
    var parentFolderId = '10I98qFF59FnUQjKv5l3bKxwmHVYrlpXV'; // Hauptordner für die Bilder
    var parentFolder = DriveApp.getFolderById(parentFolderId);
    var targetFolder;
    
    var folderIterator = parentFolder.getFoldersByName(folderIdFromForm);
    
    if (folderIterator.hasNext()) {
      targetFolder = folderIterator.next();
    } else {
      targetFolder = parentFolder.createFolder(folderIdFromForm);
    }
    
    var contentType = fileData.type;
    var bytes = Utilities.base64Decode(fileData.base64);
    var blob = Utilities.newBlob(bytes, contentType, fileData.name);
    var file = targetFolder.createFile(blob);
    
    file.setSharing(DriveApp.Access.ANYONE_WITH_LINK, DriveApp.Permission.VIEW);
    
    return {
      status: 'success',
      fileName: file.getName(),
      fileUrl: file.getUrl(),
      folderName: targetFolder.getName()
    };
    
  } catch (error) {
    return {
      status: 'error',
      message: error.toString()
    };
  }
}


// ==========================================
// 3. FUNKTIONEN FÜR DAS ADMIN-DASHBOARD
// ==========================================

// Speichert ein neues Projekt im Google Sheet inkl. UK-Dateiupload
function saveNewProject(data) {
  try {
    var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName('Projektliste');
    var lastRow = sheet.getLastRow();
    var newRow = lastRow + 1;
    
    var finalUkLink = "";
    
    // Prüfen, ob ein Umsetzungskonzept (UK) hochgeladen wurde
    if (data.ukFile && data.ukFile.base64) {
      // Ziel-Ordner-ID für UKs
      var folderId = '1uKckGJ81X-BFDmJ-6k-09JvungwkYhX0';
      var folder = DriveApp.getFolderById(folderId);
      
      var bytes = Utilities.base64Decode(data.ukFile.base64);
      var blob = Utilities.newBlob(bytes, data.ukFile.type, data.ukFile.name);
      
      // Datei im Ordner erstellen
      var file = folder.createFile(blob);
      
      // Link-Freigabe erteilen (damit die Techniker die Datei im Dashboard öffnen können)
      file.setSharing(DriveApp.Access.ANYONE_WITH_LINK, DriveApp.Permission.VIEW);
      
      // Den Link abrufen
      finalUkLink = file.getUrl();
    }
    
    // Bereite das Array für die Spalten B bis I vor
    var rowData = [
      data.name,            // Spalte B: Projektname
      data.id,              // Spalte C: Ordner ID
      finalUkLink,          // Spalte D: Der generierte Link zum PDF (oder leer)
      data.ansprechpartner, // Spalte E: Ansprechpartner
      data.telefon,         // Spalte F: Telefon
      data.email,           // Spalte G: Email
      data.adresse,         // Spalte H: Adresse
      data.notizen          // Spalte I: Notizen
    ];
    
    // Füge die Daten in die nächste freie Zeile ab Spalte 2 (B) ein
    sheet.getRange(newRow, 2, 1, 8).setValues([rowData]);
    
    return {
      status: 'success',
      message: 'Projekt erfolgreich angelegt!'
    };
  } catch (error) {
    return {
      status: 'error',
      message: 'Fehler beim Speichern: ' + error.toString()
    };
  }
}
