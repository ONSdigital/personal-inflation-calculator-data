# PIC inflation data builder

Builds a single JSON dataset for the Personal Inflation Calculator interactive from public ONS time-series data pages.

## Structure

- `config/categories.json`: locally maintained category, group, price CDID and weight CDID definitions.
- `build_pic_data.py`: downloads and transforms the ONS series.
- `data/`: generated output location.
- `requirements.txt`: Python dependencies.

The original Excel workbook is not required at runtime.

## Set-up (PowerShell)

```powershell
C:\ONSApps\My_Python\Python_3_12\python.exe -m pip install -r requirements.txt
```

## Run

From the project folder:

```powershell
C:\ONSApps\My_Python\Python_3_12\python.exe build_pic_data.py config\categories.json data\pic_data.json
```

Use `--timeout 60` if a longer request timeout is needed.

## Configuration rules

- A category with one `series` item uses that price series directly.
- A category with more than one `series` item must give every component a `weightCode`.
- Codes are ONS CDIDs and are normalised to uppercase by the script.
- The output contains the latest five-year inclusive window available for each category.
- An aggregate month is omitted if any component value or corresponding annual weight is unavailable.

## Output

The generated JSON contains build metadata and a flat `categories` array. Each category includes its ID, name, source-series mappings and monthly date/value observations.
