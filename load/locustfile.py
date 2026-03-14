from locust import HttpUser, task, between

class TeaStoreUser(HttpUser):
    wait_time = between(1, 2)

    @task
    def browse(self):
        self.client.get("/tools.descartes.teastore.webui/")