-- Pack Station: starts the Pack Station in the background if it isn't running, then opens it.
set installDir to "__INSTALL_DIR__"
set isUp to true
try
	do shell script "curl -sf -o /dev/null -m 2 http://localhost:8765/"
on error
	set isUp to false
end try
if not isUp then
	do shell script "cd " & quoted form of installDir & " && mkdir -p data && nohup .venv/bin/python -m packstation --no-browser >> data/packstation.log 2>&1 &"
	repeat 20 times
		delay 1
		try
			do shell script "curl -sf -o /dev/null -m 2 http://localhost:8765/"
			exit repeat
		end try
	end repeat
end if
open location "http://localhost:8765"
