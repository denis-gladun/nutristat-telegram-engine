function onOpen() {
  var ui = SpreadsheetApp.getUi();
  ui.createMenu('🍽️ Мои Скрипты')
      .addItem('Сохранить блюдо', 'saveDish')
      .addItem('Сохранить и очистить текущий день', 'saveAndClearCurrentDay')
      .addItem('Очистить конструктор', 'clearConstructor')
      .addToUi();
}

// 1. Функция сохранения блюда
function saveDish() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var dashboardSheet = ss.getSheetByName("Dashboard");
  var dishSheet = ss.getSheetByName("Блюдо");
  
  if (!dashboardSheet || !dishSheet) {
    SpreadsheetApp.getUi().alert("Ошибка: Не найден лист Dashboard или Блюдо.");
    return;
  }
  
  var dishName = dashboardSheet.getRange("M18").getValue();
  var statsRange = dashboardSheet.getRange("O18:S18").getValues()[0];
  
  if (!dishName || dishName.toString().trim() === "") {
    SpreadsheetApp.getUi().alert("Ошибка: Пожалуйста, введите название блюда в ячейку M18.");
    return;
  }
  
  var rowData = [
    dishName, 
    statsRange[0], // Граммы
    statsRange[1], // Ккал
    statsRange[2], // Белки
    statsRange[3], // Жиры
    statsRange[4]  // Углеводы
  ];
  
  dishSheet.appendRow(rowData);
  SpreadsheetApp.getUi().alert("Блюдо '" + dishName + "' успешно сохранено в лист «Блюдо»!");
}

/** 
 * 2. Сохранить и очистить текущий день 
 */
function saveAndClearCurrentDay() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sheet = ss.getSheetByName("Dashboard");
  const historySheet = ss.getSheetByName("История");
  
  const now = new Date();
  // 1. Получаем текущий день и месяц (по Киевскому времени)
  const day = parseInt(Utilities.formatDate(now, "GMT+3", "d"));
  const month = parseInt(Utilities.formatDate(now, "GMT+3", "M")) - 1; // 0 - Январь, 11 - Декабрь
  
  // 2. Определяем ячейку для сохранения на основе дня (для Дашборда)
  let row, col;
  if (day <= 10) {
    row = 4 + day;       // Начинается с 5-й строки
    col = 22;            // Столбец V (Ккал)
  } else if (day <= 20) {
    row = 4 + (day - 10); // Снова с 5-й строки
    col = 27;            // Столбец AA (Ккал)
  } else {
    row = 4 + (day - 20); // Снова с 5-й строки
    col = 32;            // Столбец AF (Ккал)
  }
  
  // 3. Сохраняем значения КБЖУ из "Итог дня" (G18:J18) в правый блок Дашборда
  const totals = sheet.getRange("G18:J18").getValues();
  sheet.getRange(row, col, 1, 4).setValues(totals);
  
  // 4. Сохраняем данные в лист "История"
  const weight = sheet.getRange("C18").getValue(); // Берем вес из C18
  const startCol = (month * 6) + 1; // Вычисляем стартовую колонку месяца
  const targetRow = day + 3;        // Вычисляем строку (1-е число = 4-я строка)
  
  if (historySheet) {
    // totals[0] содержит массив [Ккал, Б, Ж, У]
    historySheet.getRange(targetRow, startCol + 1, 1, 5).setValues([[totals[0][0], totals[0][1], totals[0][2], totals[0][3], weight]]);
  }
  
  // 5. Очищаем данные в таблицах (только вводимые значения)
  sheet.getRange("C5:F17").clearContent();  // Левый блок (Категории, Названия, Граммы)
  
  // Очистка новых диапазонов с итогами приемов (G5:J5, G8:J8, G11:J11)
  sheet.getRangeList(["G5:J5", "G8:J8", "G11:J11"]).clearContent();
  
  sheet.getRange("L5:O17").clearContent();  // Правый блок (Конструктор)
  sheet.getRange("C18").clearContent();     // Очищаем вес
  sheet.getRange("M18:N18").clearContent(); // Очищаем название блюда в конструкторе

  // --- НОВОЕ: Очистка диапазона на листе Agent ---
  const agentSheet = ss.getSheetByName("Agent");
  if (agentSheet) {
    agentSheet.getRange("L5:S35").clearContent();
  }
  // -----------------------------------------------
  
  // Уведомление об успешном выполнении
  SpreadsheetApp.getUi().alert("Данные за " + day + "-е число сохранены в 'Месячный подсчет' и лист 'История'. Таблицы очищены.");
}

// 3. Функция очистки правого блока (Конструктор)
function clearConstructor() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = ss.getSheetByName("Dashboard");
  if (!sheet) return;
  
  // Очищаем значения ингредиентов (категории, наименования, термо, граммы)
  sheet.getRange("L5:O17").clearContent();
  
  // Очищаем название блюда внизу
  sheet.getRange("M18:N18").clearContent();
  
  // Возвращаем прочерки по умолчанию в колонку Термо (N5:N17)
  var dashes = [];
  for (var i = 0; i < 13; i++) {
    dashes.push(["-"]);
  }
  sheet.getRange("N5:N17").setValues(dashes);
}

// 4. Функция автоматической подстановки веса при выборе блюда (Dashboard + Agent)
function onEdit(e) {
  if (!e || !e.range) return;
  const sheet = e.source.getActiveSheet();
  const sheetName = sheet.getName();
  
  if (sheetName !== 'Dashboard' && sheetName !== 'Agent') return;
  
  const row = e.range.getRow();
  const col = e.range.getColumn();
  const name = e.value;
  
  if (sheetName === 'Dashboard') {
    if (col === 4 && row >= 5 && row <= 17) {
      const category = sheet.getRange(row, 3).getValue();
      if (category === 'Блюдо' && name) {
        autoFillDishWeight(e.source, sheet, name, row, 6);
      }
    }
  }
  
  if (sheetName === 'Agent') {
    if (col === 3 && row >= 5) {
      const category = sheet.getRange(row, 2).getValue();
      if (category === 'Блюдо' && name) {
        autoFillDishWeight(e.source, sheet, name, row, 5);
      }
    }
  }
}

function autoFillDishWeight(ss, sheet, dishName, row, targetCol) {
  const dishSheet = ss.getSheetByName('Блюдо');
  if (!dishSheet) return;
  
  const data = dishSheet.getRange('A2:B').getValues();
  for (let i = 0; i < data.length; i++) {
    if (data[i][0] === dishName) {
      const defaultGrams = data[i][1];
      if (defaultGrams) {
        sheet.getRange(row, targetCol).setValue(defaultGrams);
      }
      break;
    }
  }
}