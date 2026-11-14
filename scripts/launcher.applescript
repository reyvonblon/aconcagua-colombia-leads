on run
	set projectDir to "/Users/raymondvonblon/.gemini/antigravity/scratch/aconcagua-leads"
	set portUrl to "http://127.0.0.1:8796/admin"
	set healthUrl to "http://127.0.0.1:8796/api/health"

	try
		do shell script "curl -fsS --noproxy '*' " & quoted form of healthUrl & " >/dev/null 2>&1"
	on error
		do shell script "cd " & quoted form of projectDir & " && nohup /usr/bin/python3 webapp/server.py >/tmp/aconcagua_colombia.log 2>&1 </dev/null &"
		delay 1.5
	end try

	open location portUrl
end run
